"""Daemon side of tier-2 data exchange (spec §5.2): prepare a run directory, ingest outputs, GC.

Seams for run_code (b98.4):

    rx = RunExchange(store, runs_root(settings.data_dir), unit_of=catalog_unit)
    run_dir = rx.prepare_run(node_id, inputs)      # set TN_RUN_DIR=run_dir in the kernel
    ...execute...
    result = rx.ingest_run(node_id, succeeded=ok)  # datasets + issues for the code node
    rx.gc(keep=lambda node_id: node_is_referenced(node_id))

Lineage: an output's parents are all declared inputs unless the code narrowed them with
meta["parents"] (validated as a subset). Tracking actual reads would be weaker evidence:
an input can shape a result without being read through tn (e.g. a value Claude typed after
looking at it), so the declared set is the honest superset; narrowing is explicit. A fit's
prediction also gets the fit's dataset as a parent. producer = {"kind": "code", "node", "output"}.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore, Lineage
from telemetry_nerd.exchange import fmt
from telemetry_nerd.exchange.fmt import ExchangeError
from telemetry_nerd.exchange.tables import duplicates, normalize_output, read_ipc, write_tables
from telemetry_nerd.model.distribution import COLUMN_SCHEMA, DIST_SCHEMA, BucketScheme, DistResult
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange


def runs_root(data_dir: Path) -> Path:
    return Path(data_dir) / "runs"


@dataclass(frozen=True)
class Ingested:
    name: str  # output name in the run (tn.put return value)
    dataset_id: str
    representation: str
    rows: int
    parents: tuple[str, ...]
    caveats: tuple[str, ...]  # includes the uncertainty status (spec §5.3), if any

    @property
    def uncertainty(self) -> str | None:
        """The output's uncertainty status (fmt.UNCERTAINTY_STATUS), None when clean."""
        return next((c for c in self.caveats if c in fmt.UNCERTAINTY_STATUS), None)


@dataclass(frozen=True)
class Issue:
    name: str  # output name or file name
    code: str  # run_failed | uncommitted | partial_write | corrupt | invalid
    message: str


@dataclass(frozen=True)
class IngestResult:
    datasets: list[Ingested] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


def _caveats(meta: DatasetMeta) -> list[str]:
    out = list(meta.source_caveats)
    if meta.partial:
        out.append("partial")
    if meta.failed_spans:
        out.append("failed_spans")
    return list(dict.fromkeys(out))


class RunExchange:
    def __init__(
        self,
        store: DatasetStore,
        root: Path,
        unit_of: Callable[[DatasetMeta], str | None] | None = None,
    ) -> None:
        self.store = store
        self.root = Path(root)
        self._unit_of = unit_of or (lambda m: m.unit)

    def run_dir(self, code_node_id: str) -> Path:
        return self.root / fmt.check_id(code_node_id, "code node id")

    # --- export -------------------------------------------------------------------------

    def prepare_run(self, code_node_id: str, input_handles: Iterable[str]) -> Path:
        """Fresh run dir with the declared inputs exported; run.json is written last. A re-run
        of the same node replaces the previous inputs/outputs (kernels holding old mmaps keep
        the old inodes)."""
        handles = list(dict.fromkeys(input_handles))
        for h in handles:
            fmt.check_id(h, "input handle")
        run = self.run_dir(code_node_id)
        if run.exists():
            shutil.rmtree(run)
        (run / fmt.INPUTS).mkdir(parents=True)
        (run / fmt.OUTPUTS).mkdir()
        try:
            for h in handles:
                self._export(run / fmt.INPUTS, h)
            fmt.write_json_atomic(
                run / fmt.MANIFEST,
                {"version": fmt.VERSION, "code_node": code_node_id, "inputs": handles},
            )
        except BaseException:
            shutil.rmtree(run, ignore_errors=True)
            raise
        return run

    def _export(self, inputs_dir: Path, handle: str) -> None:
        meta = self.store.meta(handle)
        rep = meta.representation
        tables: dict[str, pa.Table] = {}
        if rep in fmt.TIME_SERIES:
            _, res = self.store.get(handle)
            rows = res.buckets
            interval = self.store.interval(handle)
            if interval is not None:
                rows = rows.append_column("lo", interval.column("lo")).append_column(
                    "hi", interval.column("hi")
                )
            tables = {"rows": rows, "series": res.series}
        elif rep == fmt.DISTRIBUTION:
            _, dist = self.store.get_distribution(handle)
            tables = {"rows": dist.rows, "columns": dist.columns, "series": dist.series}
        elif rep != fmt.ESTIMATE:
            raise ExchangeError(
                f"{handle} is a {rep} dataset; tier-2 inputs can be {', '.join(fmt.EXPORTABLE)}"
            )
        files, _ = write_tables(inputs_dir, handle, tables)
        doc = {k: v for k, v in meta.to_dict().items() if k != "id"}
        doc |= {
            "version": fmt.VERSION,
            "handle": handle,
            "unit": self._unit_of(meta),
            "caveats": _caveats(meta),
            "tables": files,
            "rows": tables["rows"].num_rows if tables else 0,
        }
        fmt.write_json_atomic(inputs_dir / fmt.meta_file(handle), doc)

    # --- ingest -------------------------------------------------------------------------

    def ingest_run(self, code_node_id: str, *, succeeded: bool) -> IngestResult:
        """Store the committed outputs of a finished run as datasets.

        A failed/killed run ingests nothing (its outputs may describe a half-done
        computation); uncommitted or half-written files are reported, never ingested.
        Idempotent per output: ingested.json records what is already stored."""
        run = self.run_dir(code_node_id)
        manifest = fmt.read_manifest(run)
        out_dir = run / fmt.OUTPUTS
        committed, issues = _scan(out_dir)
        if not succeeded:
            return IngestResult(
                [],
                issues + [
                    Issue(n, "run_failed", "the run failed; its outputs are not ingested")
                    for n in committed
                ],
            )  # fmt: skip
        done = _read_ingested(run)
        ctx = _Ctx(code_node_id, manifest["inputs"], run / fmt.INPUTS)
        metas: dict[str, dict] = {}
        for name in committed:
            try:
                metas[name] = fmt.read_json(out_dir / fmt.meta_file(name))
            except (OSError, ValueError) as e:
                issues.append(Issue(name, "corrupt", f"meta unreadable: {e}"))
        result: list[Ingested] = []
        # fits first: a prediction names its fit as a parent
        for name in sorted(metas, key=lambda n: (metas[n].get("kind") != "fit", n)):
            if name in done:
                continue
            try:
                ing = self._ingest_one(ctx, out_dir, name, metas[name], done)
            except ExchangeError as e:
                issues.append(Issue(name, "invalid", str(e)))
                continue
            except (OSError, pa.ArrowInvalid) as e:
                issues.append(Issue(name, "corrupt", f"{type(e).__name__}: {e}"))
                continue
            result.append(ing)
            done[name] = ing.dataset_id
            fmt.write_json_atomic(run / fmt.INGESTED, done)
        return IngestResult(result, issues)

    def _ingest_one(self, ctx: _Ctx, out_dir: Path, name: str, raw: dict, done: dict) -> Ingested:
        if raw.get("version") != fmt.VERSION:
            raise ExchangeError(f"meta version {raw.get('version')!r}, expected {fmt.VERSION}")
        if raw.get("name") != name:
            raise ExchangeError(f"meta names output {raw.get('name')!r}, file is {name!r}")
        producer = {"kind": "code", "node": ctx.node, "output": name}
        if raw.get("kind") == "fit":
            return self._ingest_fit(ctx, name, raw, producer)
        if raw.get("kind") != "dataset":
            raise ExchangeError(f"unknown output kind {raw.get('kind')!r}")
        return self._ingest_dataset(ctx, out_dir, name, raw, producer, done)

    def _ingest_dataset(
        self, ctx: _Ctx, out_dir: Path, name: str, raw: dict, producer: dict, done: dict
    ) -> Ingested:
        meta = fmt.normalize_output_meta(raw, ctx.declared, ctx.input_meta, internal=True)
        tables = normalize_output(meta, _read_tables(out_dir, raw))
        parents = list(meta["parents"])
        if meta.get("prediction_of"):
            fit_id = done.get(meta["prediction_of"])
            if fit_id is None:
                raise ExchangeError(
                    f"prediction of {meta['prediction_of']!r}, which was not ingested"
                )
            parents.insert(0, fit_id)
        uncertainty = {"exact": True} if meta["exact"] else meta["uncertainty"]
        if meta["description"]:
            producer["description"] = meta["description"]
        lineage = Lineage(
            producer=producer,
            parents=tuple(parents),
            unit=meta["unit"],
            uncertainty=uncertainty,
            caveats=ctx.caveats(meta["parents"], meta["caveats"], uncertainty is None),
        )
        rng = _range(meta, tables["rows"])
        source = ctx.source(meta["parents"], meta["like"])
        expr = f"code:{ctx.node}/{name}"
        rows, series = ctx.identify(name, tables["rows"], meta)
        if meta["representation"] == fmt.DISTRIBUTION:
            columns = tables.get("columns")
            if columns is not None:
                columns, _ = ctx.identify(name, columns, meta)
            else:  # n per step from the rows (steps with n = 0 are not recoverable)
                columns = rows.group_by(["ts_ms", "series_id"]).aggregate([("count", "sum")])
                columns = columns.rename_columns(["ts_ms", "series_id", "n"])
            scheme = BucketScheme.from_dict(meta["scheme"]) if meta["scheme"] else _scheme(rows)
            stored = self.store.put_distribution(
                source=source,
                rng=rng,
                step_ms=meta["step_ms"],
                resolution_ms=meta["resolution_ms"],
                dist=DistResult(
                    rows.select(DIST_SCHEMA.names).cast(DIST_SCHEMA),
                    columns.select(COLUMN_SCHEMA.names).cast(COLUMN_SCHEMA),
                    series,
                    scheme,
                    expr,
                ),
                histogram=None,
                n_min=meta["n_min"] or 1,
                lineage=lineage,
            )
        else:
            stored = self.store.put(
                source=source,
                expr=expr,
                rng=rng,
                step_ms=meta["step_ms"],
                resolution_ms=meta["resolution_ms"],
                result=FetchResult(_bucket_rows(rows, uncertainty), series),
                representation=meta["representation"],
                semantics_flags=meta["semantics_flags"],
                lineage=lineage,
            )
        return Ingested(
            name, stored.id, stored.representation, rows.num_rows, tuple(parents),
            tuple(stored.source_caveats),
        )  # fmt: skip

    def _ingest_fit(self, ctx: _Ctx, name: str, raw: dict, producer: dict) -> Ingested:
        if raw.get("files"):
            raise ExchangeError("a fit output carries no tables")
        fit = fmt.normalize_fit(raw.get("fit") or {})
        meta = fmt.normalize_fit_meta(raw, ctx.declared, ctx.input_meta)
        parents = meta["parents"]
        if meta["start_ms"] is not None:
            rng = TimeRange(meta["start_ms"], meta["end_ms"])
        elif parents:
            pm = [ctx.input_meta(p) for p in parents]
            rng = TimeRange(min(m["start_ms"] for m in pm), max(m["end_ms"] for m in pm))
        else:
            raise ExchangeError("a fit without inputs needs start_ms/end_ms (its fit range)")
        stored = self.store.put(
            source=ctx.source(parents),
            expr=f"code:{ctx.node}/{name}",
            rng=rng,
            step_ms=rng.end_ms - rng.start_ms,
            resolution_ms=rng.end_ms - rng.start_ms,
            result=FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table()),
            representation=fmt.ESTIMATE,
            lineage=Lineage(
                producer=producer,
                parents=tuple(parents),
                uncertainty=None,
                fit=fit,
                caveats=ctx.caveats(
                    parents, meta["caveats"], bool(fit["params_without_uncertainty"])
                ),
            ),
        )
        return Ingested(
            name, stored.id, fmt.ESTIMATE, 0, tuple(parents), tuple(stored.source_caveats)
        )

    # --- GC ---------------------------------------------------------------------------

    def gc(self, keep: Callable[[str], bool]) -> list[str]:
        """Remove run dirs whose code node `keep(node_id)` rejects (policy: the caller's, e.g.
        node unreferenced and not running). Returns the removed node ids."""
        if not self.root.exists():
            return []
        removed = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and not keep(d.name):
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)
        return removed


# --- helpers -------------------------------------------------------------------------------


def _scan(out_dir: Path) -> tuple[list[str], list[Issue]]:
    """Committed output names (meta file present) and issues for everything else."""
    if not out_dir.exists():
        return [], []
    committed, issues, data = [], [], set()
    for p in sorted(out_dir.iterdir()):
        if p.name.startswith(fmt.TMP_PREFIX):
            issues.append(Issue(p.name, "partial_write", "half-written file (writer died)"))
        elif p.name.endswith(fmt.META_SUFFIX):
            committed.append(p.name[: -len(fmt.META_SUFFIX)])
        elif p.name.endswith(fmt.ARROW_SUFFIX):
            data.add(p.name.split(".", 1)[0])
    for n in sorted(data - set(committed)):
        issues.append(Issue(n, "uncommitted", "data without a meta file: tn.put did not finish"))
    return committed, issues


def _read_ingested(run: Path) -> dict[str, str]:
    p = run / fmt.INGESTED
    return fmt.read_json(p) if p.exists() else {}


def _read_tables(out_dir: Path, raw: dict) -> dict[str, pa.Table]:
    files = raw.get("files")
    if not isinstance(files, dict) or "rows" not in files:
        raise ExchangeError("meta.files must name the output's tables")
    tables, size = {}, 0
    for t, f in files.items():
        if f != fmt.table_file(raw["name"], t):
            raise ExchangeError(f"unexpected file name {f!r} for table {t!r}")
        path = out_dir / f
        size += path.stat().st_size
        tables[t] = read_ipc(path)
    if size != raw.get("bytes") or tables["rows"].num_rows != raw.get("rows"):
        raise ExchangeError("tables do not match their meta (size/rows): not a complete write")
    return tables


def _range(meta: dict, rows: pa.Table) -> TimeRange:
    if meta["start_ms"] is not None:
        return TimeRange(meta["start_ms"], meta["end_ms"])
    if rows.num_rows == 0:
        raise ExchangeError("an empty output needs start_ms/end_ms (or like=<input>)")
    ts = rows.column("ts_ms")
    lo, hi = pc.min(ts).as_py(), pc.max(ts).as_py()
    # bucket grids are inclusive of both ends (as a query's): the first bucket ends at start_ms;
    # a single bucket needs a range, so it reaches back one step
    return TimeRange(lo if hi > lo else lo - meta["step_ms"], hi)


def _bucket_rows(rows: pa.Table, uncertainty: dict | None) -> pa.Table:
    n = rows.num_rows
    cols = {
        name: rows.column(name) if name in rows.column_names else pa.nulls(n, f.type)
        for name, f in zip(BUCKET_SCHEMA.names, BUCKET_SCHEMA, strict=True)
    }  # a value code did not give (min/max/count) is unknown: null, never invented
    out = pa.table(cols).cast(BUCKET_SCHEMA)
    if uncertainty is not None and not uncertainty.get("exact"):
        out = out.append_column("lo", rows.column("lo")).append_column("hi", rows.column("hi"))
    return out


def _scheme(rows: pa.Table) -> BucketScheme:
    edges = pc.unique(pa.concat_arrays([
        pc.drop_null(rows.column("bucket_lo")).combine_chunks(),
        pc.drop_null(rows.column("bucket_hi")).combine_chunks(),
    ])).to_pylist()  # fmt: skip
    finite = sorted(e for e in edges if abs(e) != float("inf"))
    return BucketScheme("custom", tuple(finite)) if finite else BucketScheme("none")


class _Ctx:
    """What ingest needs to know about the run's inputs (read back from the run dir, which
    is what the kernel saw)."""

    def __init__(self, node: str, declared: list[str], inputs_dir: Path) -> None:
        self.node = node
        self.declared = list(declared)
        self._dir = inputs_dir
        self._metas: dict[str, dict] = {}
        self._labels: dict[str, str] | None = None

    def input_meta(self, handle: str) -> dict:
        if handle not in self.declared:
            raise fmt.undeclared_error(handle, self.declared)
        if handle not in self._metas:
            self._metas[handle] = fmt.read_json(self._dir / fmt.meta_file(handle))
        return self._metas[handle]

    def parent_caveats(self, parents: list[str]) -> list[str]:
        return [c for p in parents for c in self.input_meta(p).get("caveats", [])]

    def caveats(self, parents: list[str], own: list[str], no_uncertainty: bool) -> tuple[str, ...]:
        """An output's caveats: its parents', its own, and no_uncertainty when it applies."""
        out = self.parent_caveats(parents) + own
        if no_uncertainty:
            out.append(fmt.NO_UNCERTAINTY)
        return tuple(dict.fromkeys(out))

    def source(self, parents: list[str], like: str | None = None) -> str:
        """`like`'s source, else the parents' common source, else "code"."""
        if like:
            return self.input_meta(like)["source"]
        sources = {self.input_meta(p)["source"] for p in parents}
        return sources.pop() if len(sources) == 1 else "code"

    def _input_labels(self) -> dict[str, str]:
        if self._labels is None:
            self._labels = {}
            for h in self.declared:
                f = self.input_meta(h).get("tables", {}).get("series")
                if f:
                    t = read_ipc(self._dir / f)
                    self._labels.update(
                        zip(t.column("series_id").to_pylist(), t.column("labels").to_pylist(), strict=True)
                    )  # fmt: skip
        return self._labels

    def identify(self, name: str, rows: pa.Table, meta: dict) -> tuple[pa.Table, pa.Table]:
        """Derived series identity = hash(code node, output labels) (spec §3.1). Labels come
        from meta.labels columns, an input's series_id (its labels), or none (one series)."""
        source = f"code:{self.node}"
        labels = meta.get("labels") or []
        if labels:
            keys = [
                labels_json(dict(zip(labels, vals, strict=True)))
                for vals in zip(*(rows.column(c).to_pylist() for c in labels), strict=True)
            ]
            rows = rows.drop_columns(labels)
        elif "series_id" in rows.column_names:
            known = self._input_labels()
            keys = []
            for sid in rows.column("series_id").to_pylist():
                if sid not in known:
                    raise ExchangeError(
                        f"{name}: series_id {sid!r} is not a series of any declared input; "
                        "name series with label columns (meta.labels) instead"
                    )
                keys.append(known[sid])
            rows = rows.drop_columns(["series_id"])
        else:
            keys = [labels_json({})] * rows.num_rows
        ids = {k: series_id(source, json.loads(k)) for k in set(keys)}
        rows = rows.append_column("series_id", pa.array([ids[k] for k in keys], pa.string()))
        extra = ["bucket_lo"] if "bucket_lo" in rows.column_names else []
        try:
            duplicates(rows, ["series_id", "ts_ms", *extra], name)
        except ExchangeError as e:
            raise ExchangeError(f"{e} (inputs' series with equal labels collide)") from e
        series = pa.table(
            {"series_id": [ids[k] for k in sorted(ids)], "labels": sorted(ids)},
            schema=SERIES_SCHEMA,
        )
        return rows, series
