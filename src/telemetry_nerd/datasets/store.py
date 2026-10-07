"""Immutable datasets: a snapshot of buckets for (source, expr, range, step)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field

import duckdb
import pyarrow as pa

from telemetry_nerd.datasets.db import fetch_arrow, upsert_series
from telemetry_nerd.model.distribution import COLUMN_SCHEMA, DIST_SCHEMA, BucketScheme, DistResult
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.model.time import TimeRange, now_ms


@dataclass(frozen=True)
class DatasetMeta:
    id: str
    source: str
    expr: str
    start_ms: int
    end_ms: int
    step_ms: int
    resolution_ms: int
    representation: str = "bucket_agg"
    created_at_ms: int = 0
    partial: int = 0  # incomplete source cells dropped while fetching
    quantile: float | None = None  # q of a quantile dataset
    n_min: int | None = None  # observations per bucket for a meaningful quantile
    scheme: dict | None = None  # bucket scheme of a distribution dataset
    histogram: dict | None = None  # {"selector", "by"}: the histogram a distribution came from
    source_caveats: list[str] = field(default_factory=list)  # conversion caveats from the source
    derived: dict | None = None  # filter() output: {op, from, label, reason, period_ms, ...}
    failed_spans: list[list] = field(default_factory=list)  # [[a, b, reason]] fetch failures
    # source semantics that shape how values read, recorded at query time (the dataset is a
    # snapshot; the source may be reconfigured): {"post_gap_increase_spike": True}
    semantics_flags: dict = field(default_factory=dict)
    # Tier-2 code outputs (spec §5.2). Lineage: the producing code node and the input datasets.
    producer: dict | None = None  # {"kind": "code", "node", "output", "description"}
    parents: list[str] = field(default_factory=list)
    unit: str | None = None  # declared by the producer; None = look it up in the catalog
    # declared uncertainty: {"method", "level"} with lo/hi per row, or {"exact": True};
    # None = undeclared (a code output then carries the no_uncertainty caveat)
    uncertainty: dict | None = None
    fit: dict | None = None  # representation "estimate": {model, method, params, ...}
    #: the language `expr` is written in, so show, notes and analysis never re-parse an
    #: Elasticsearch expr as PromQL (even after the source is disconnected)
    query_language: str = "promql"

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def code_node(self) -> str | None:
        """The code node that produced this dataset (directly, or through a filter of a code
        output), or None for data fetched from a source. A code output is fixed data: its
        `expr` names the output, not a query, so nothing may re-fetch it from `source`."""
        if self.producer and self.producer.get("kind") == "code":
            return self.producer.get("node")
        return None


# the expression of a code output as stored (exchange: f"code:{node}/{name}"); never PromQL
CODE_EXPR = re.compile(r"^code:[A-Za-z0-9][A-Za-z0-9_-]{0,63}/[a-z][a-z0-9_]{0,63}$")


def is_code_expr(expr: str) -> bool:
    return bool(CODE_EXPR.match(expr.strip()))


@dataclass(frozen=True)
class Lineage:
    """Provenance and declared semantics of a derived dataset (tier-2 code outputs)."""

    producer: dict
    parents: tuple[str, ...] = ()
    unit: str | None = None
    uncertainty: dict | None = None
    fit: dict | None = None
    caveats: tuple[str, ...] = ()

    def fields(self) -> dict:
        return {
            "producer": dict(self.producer),
            "parents": list(self.parents),
            "unit": self.unit,
            "uncertainty": dict(self.uncertainty) if self.uncertainty else None,
            "fit": self.fit,
        }


def _union(*groups) -> list[str]:
    return list(dict.fromkeys(c for g in groups for c in g))


INTERVAL_SCHEMA = pa.schema(
    [("ts_ms", pa.int64()), ("series_id", pa.string()), ("lo", pa.float64()), ("hi", pa.float64())]
)


class DatasetStore:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        new_id: Callable[[str], str],
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._con = con
        self._new_id = new_id
        self._clock = clock

    def put(
        self,
        *,
        source: str,
        expr: str,
        rng: TimeRange,
        step_ms: int,
        resolution_ms: int,
        result: FetchResult,
        representation: str = "bucket_agg",
        quantile: float | None = None,
        n_min: int | None = None,
        histogram: dict | None = None,
        derived: dict | None = None,
        semantics_flags: dict | None = None,
        lineage: Lineage | None = None,
        caveats: Sequence[str] = (),
        query_language: str = "promql",
    ) -> DatasetMeta:
        meta = DatasetMeta(
            id=self._new_id("d"),
            source=source,
            expr=expr,
            start_ms=rng.start_ms,
            end_ms=rng.end_ms,
            step_ms=step_ms,
            resolution_ms=resolution_ms,
            created_at_ms=self._clock(),
            partial=result.partial,
            representation=representation,
            quantile=quantile,
            n_min=n_min,
            histogram=histogram,
            derived=derived,
            failed_spans=[list(f) for f in result.failed],
            semantics_flags=dict(semantics_flags or {}),
            query_language=query_language,
            source_caveats=_union(
                lineage.caveats if lineage else (),
                caveats,
                [f"source_warning:{n}" for n in result.notes],
            ),
            **(lineage.fields() if lineage else {}),
        )
        names = result.buckets.column_names
        has_interval = "lo" in names and "hi" in names
        if has_interval != bool(meta.uncertainty and not meta.uncertainty.get("exact")):
            raise ValueError("lo/hi columns go with a declared interval uncertainty, and only then")
        cols = "ts_ms, series_id, avg, min, max, count" + (", lo, hi" if has_interval else "")
        con = self._con
        con.begin()
        try:
            self._insert_meta(meta)
            con.register("_tn_ds", result.buckets)
            try:
                con.execute(
                    f"""INSERT INTO dataset_rows (dataset_id, {cols})
                        SELECT $id, {cols} FROM _tn_ds""",
                    {"id": meta.id},
                )
            finally:
                con.unregister("_tn_ds")
            upsert_series(con, result.series)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return meta

    def _insert_meta(self, meta: DatasetMeta) -> None:
        self._con.execute(
            "INSERT INTO datasets VALUES ($id, $m)",
            {"id": meta.id, "m": json.dumps(meta.to_dict())},
        )

    def meta(self, dataset_id: str) -> DatasetMeta:
        row = self._con.execute(
            "SELECT meta FROM datasets WHERE id = $id", {"id": dataset_id}
        ).fetchone()
        if row is None:
            raise NotFound(f"dataset {dataset_id} not found")
        return DatasetMeta(**json.loads(row[0]))

    def list_metas(self) -> list[DatasetMeta]:
        rows = self._con.execute("SELECT meta FROM datasets").fetchall()
        return [DatasetMeta(**json.loads(r[0])) for r in rows]

    def exists(self, dataset_id: str) -> bool:
        return (
            self._con.execute(
                "SELECT 1 FROM datasets WHERE id = $id", {"id": dataset_id}
            ).fetchone()
            is not None
        )

    def get(self, dataset_id: str) -> tuple[DatasetMeta, FetchResult]:
        meta = self.meta(dataset_id)
        if meta.representation == "distribution":
            raise ValueError(f"dataset {dataset_id} is a distribution; use get_distribution")
        params = {"id": dataset_id}
        buckets = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, avg, min, max, count FROM dataset_rows
               WHERE dataset_id = $id ORDER BY series_id, ts_ms""",
            params,
            BUCKET_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM dataset_rows WHERE dataset_id = $id)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        return meta, FetchResult(buckets, series)

    def interval(self, dataset_id: str) -> pa.Table | None:
        """Per-row declared [lo, hi] (INTERVAL_SCHEMA), or None when no interval is declared."""
        meta = self.meta(dataset_id)
        if not meta.uncertainty or meta.uncertainty.get("exact"):
            return None
        return fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, lo, hi FROM dataset_rows
               WHERE dataset_id = $id ORDER BY series_id, ts_ms""",
            {"id": dataset_id},
            INTERVAL_SCHEMA,
        )

    def put_distribution(
        self,
        *,
        source: str,
        rng: TimeRange,
        step_ms: int,
        resolution_ms: int,
        dist: DistResult,
        histogram: dict | None,
        n_min: int,
        lineage: Lineage | None = None,
        query_language: str = "promql",
    ) -> DatasetMeta:
        if lineage and lineage.uncertainty and not lineage.uncertainty.get("exact"):
            raise ValueError("a distribution has no lo/hi columns: only exact or undeclared")
        meta = DatasetMeta(
            id=self._new_id("d"),
            source=source,
            expr=dist.expr,
            start_ms=rng.start_ms,
            end_ms=rng.end_ms,
            step_ms=step_ms,
            resolution_ms=resolution_ms,
            representation="distribution",
            created_at_ms=self._clock(),
            n_min=n_min,
            scheme=dist.scheme.to_dict(),
            histogram=histogram,
            failed_spans=[list(f) for f in dist.failed],
            query_language=query_language,
            source_caveats=_union(dist.caveats, lineage.caveats if lineage else ()),
            **(lineage.fields() if lineage else {}),
        )
        con = self._con
        con.begin()
        try:
            self._insert_meta(meta)
            for name, table, sql in (
                ("_tn_dr", dist.rows,
                 "INSERT INTO dist_rows SELECT $id, ts_ms, series_id, bucket_lo, bucket_hi, count FROM _tn_dr"),
                ("_tn_dc", dist.columns,
                 "INSERT INTO dist_columns SELECT $id, ts_ms, series_id, n FROM _tn_dc"),
            ):  # fmt: skip
                con.register(name, table)
                try:
                    con.execute(sql, {"id": meta.id})
                finally:
                    con.unregister(name)
            upsert_series(con, dist.series)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return meta

    def get_distribution(self, dataset_id: str) -> tuple[DatasetMeta, DistResult]:
        meta = self.meta(dataset_id)
        if meta.representation != "distribution":
            raise ValueError(f"dataset {dataset_id} is {meta.representation}, not a distribution")
        params = {"id": dataset_id}
        rows = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, bucket_lo, bucket_hi, count FROM dist_rows
               WHERE dataset_id = $id ORDER BY series_id, ts_ms, bucket_lo, bucket_hi""",
            params,
            DIST_SCHEMA,
        )
        columns = fetch_arrow(
            self._con,
            "SELECT ts_ms, series_id, n FROM dist_columns WHERE dataset_id = $id ORDER BY series_id, ts_ms",
            params,
            COLUMN_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM dist_columns WHERE dataset_id = $id)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        scheme = BucketScheme.from_dict(meta.scheme)
        return meta, DistResult(
            rows, columns, series, scheme, meta.expr, tuple(meta.source_caveats)
        )

    def series_count(self, dataset_id: str) -> int:
        table = (
            "dist_columns"
            if self.meta(dataset_id).representation == "distribution"
            else "dataset_rows"
        )
        row = self._con.execute(
            f"SELECT COUNT(DISTINCT series_id) FROM {table} WHERE dataset_id = $id",
            {"id": dataset_id},
        ).fetchone()
        return int(row[0]) if row else 0

    def series_labels_by_dataset(self) -> dict[str, list[dict]]:
        """dataset id -> the labels of each of its series (every dataset in the store)."""
        rows = self._con.execute(
            """SELECT d.dataset_id, s.labels FROM (
                 SELECT DISTINCT dataset_id, series_id FROM dataset_rows
                 UNION SELECT DISTINCT dataset_id, series_id FROM dist_columns) d
               JOIN series s ON s.series_id = d.series_id"""
        ).fetchall()
        out: dict[str, list[dict]] = {}
        for did, raw in rows:
            try:
                lb = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(lb, dict):
                out.setdefault(did, []).append(lb)
        return out

    def record_statistics(self, stats: list[dict]) -> None:
        """Remember evidence statistics an op emitted, with their variation `source` and how
        they were stated (interval, exact, params)."""
        rows = [
            (st["dataset"], st["name"], st.get("method") or "", float(st["value"]),
             st.get("source") or "", *_bounds(st.get("interval")), bool(st.get("exact")),
             _params_json(st.get("params")))
            for st in stats
            if isinstance(st.get("dataset"), str) and isinstance(st.get("name"), str)
            and isinstance(st.get("value"), int | float) and not isinstance(st["value"], bool)
        ]  # fmt: skip
        if rows:
            self._con.executemany(
                "INSERT OR IGNORE INTO op_statistics (dataset, name, method, value, source, lo, "
                "hi, exact, params) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def labelled_statistics(self, datasets: list[str]) -> list[dict]:
        """Evidence statistics ops emitted for `datasets` with a variation source, as citable
        statistics (the op's wording; params as recorded), in the order they were recorded."""
        if not datasets:
            return []
        marks = ", ".join("?" for _ in datasets)
        rows = self._con.execute(
            f"SELECT dataset, name, method, value, source, lo, hi, exact, params "
            f"FROM op_statistics WHERE dataset IN ({marks}) AND source != ''",
            list(datasets),
        ).fetchall()
        out = []
        for did, name, method, value, source, lo, hi, exact, params in rows:
            st: dict = {"kind": "statistic", "dataset": did, "name": name, "value": value}
            if exact:
                st["exact"] = True
            elif lo is not None and hi is not None:
                st["interval"] = [lo, hi]
            else:
                st["uncertainty_unknown"] = True
            st |= {"method": method or "op result", "source": source}
            try:
                st["params"] = json.loads(params) if params else {}
            except (TypeError, json.JSONDecodeError):
                st["params"] = {}
            out.append(st)
        return out

    def statistic_methods(self, dataset: str, name: str) -> set[str]:
        """Methods ops recorded for a statistic `name` of `dataset` (empty: none emitted it)."""
        rows = self._con.execute(
            "SELECT DISTINCT method FROM op_statistics WHERE dataset=? AND name=? AND method!=''",
            (dataset, name),
        ).fetchall()
        return {r[0] for r in rows}

    def statistic_sources(
        self, dataset: str, name: str, method: str, value: float
    ) -> set[str] | None:
        """Variation sources ('' = none) ops gave the statistic `name` of `dataset`; None when no
        op emitted it.

        (dataset, name) identifies it; the cited value then narrows the rows (exact, else equal
        to the precision it was cited at, else every value) and the method only breaks ties: a
        finding's `method` is descriptive text an agent shortens or renames ("check_littles_law"
        for the op's long METHOD), never an identity. Several sources left = the ops disagree,
        which the caller reports as undetermined."""
        rows: list[tuple[str, float, str]] = self._con.execute(
            "SELECT DISTINCT method, value, source FROM op_statistics WHERE dataset=? AND name=?",
            (dataset, name),
        ).fetchall()
        if not rows:
            return None
        rows = _narrow_by_value(rows, float(value))
        want = _norm_method(method)
        same = [r for r in rows if want and _norm_method(r[0]) == want]
        return {r[2] for r in same or rows}


def _bounds(interval) -> tuple[float | None, float | None]:
    if (
        isinstance(interval, list | tuple)
        and len(interval) == 2
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in interval)
    ):
        return float(interval[0]), float(interval[1])
    return None, None


def _params_json(params) -> str | None:
    if not isinstance(params, dict):
        return None
    try:
        return json.dumps(params, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None


def _norm_method(method: str | None) -> str:
    return " ".join((method or "").lower().split())


def _cited_precision(value: float) -> float:
    """Half a unit in the last significant digit `value` was written with (as repr prints it)."""
    text = repr(abs(value))
    if "e" in text or "inf" in text or "nan" in text:
        return 0.0
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    if frac:
        return 0.5 * 10.0 ** -len(frac)
    return 0.5 * 10.0 ** (len(whole) - len(whole.rstrip("0")))


def _narrow_by_value(
    rows: list[tuple[str, float, str]], value: float
) -> list[tuple[str, float, str]]:
    """The rows whose value is the cited one: exact, else within the cited rounding, else all."""
    exact = [r for r in rows if r[1] == value]
    if exact:
        return exact
    tol = _cited_precision(value)
    near = [r for r in rows if abs(r[1] - value) <= tol * (1 + 1e-9)]
    return near or rows
