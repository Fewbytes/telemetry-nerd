"""tn: workspace data inside a tier-2 kernel (spec §5.2).

    import telemetry_nerd.tn as tn
    tn.inputs                       # handles declared by run_code(inputs=[...])
    df = tn.dataset("d3")           # polars, memory-mapped (tn.dataset("d3", arrow=True): pyarrow)
    tn.meta("d3")                   # unit, step_ms, representation, caveats, uncertainty, ...
    tn.put(out, {"like": "d3", "uncertainty": {"method": "bootstrap", "level": 0.95}})
    tn.put_fit("linear", {"slope": {"value": 2.1, "interval": [1.8, 2.4]}}, method="OLS",
               diagnostics={"durbin_watson": 1.9})

The run directory comes from TN_RUN_DIR (set per run). Everything goes through `_transport()`
so a socket stream can replace the files later without changing these calls. Imports stay
light: pyarrow/polars load on first use.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from telemetry_nerd.exchange import fmt
from telemetry_nerd.exchange.fmt import ExchangeError as TnError

__all__ = ["TnError", "dataset", "inputs", "meta", "put", "put_fit"]  # noqa: F822 (inputs: module __getattr__)


class _FileTransport:
    """Run directory written by the daemon (inputs) and by this kernel (outputs)."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.manifest = fmt.read_manifest(run_dir)
        self.inputs: tuple[str, ...] = tuple(self.manifest.get("inputs") or ())

    def input_meta(self, handle: str) -> dict:
        self._declared(handle)
        return fmt.read_json(self.run_dir / fmt.INPUTS / fmt.meta_file(handle))

    def read_table(self, handle: str, table: str):
        from telemetry_nerd.exchange.tables import read_ipc

        tables = self.input_meta(handle).get("tables") or {}
        if table not in tables:
            raise TnError(f"{handle} has no table {table!r}; it has: {', '.join(tables) or 'none'}")
        return read_ipc(self.run_dir / fmt.INPUTS / tables[table])

    def taken(self) -> set[str]:
        out = self.run_dir / fmt.OUTPUTS
        return (
            {p.name.split(".", 1)[0] for p in out.iterdir() if not p.name.startswith(".")}
            if out.exists()
            else set()
        )

    def write_output(self, name: str, tables: Mapping[str, Any], meta: dict) -> None:
        from telemetry_nerd.exchange.tables import write_ipc

        out = self.run_dir / fmt.OUTPUTS
        out.mkdir(exist_ok=True)
        files, size = {}, 0
        for t, table in tables.items():
            files[t] = fmt.table_file(name, t)
            size += write_ipc(out / files[t], table)
        rows = tables["rows"].num_rows if "rows" in tables else 0
        # the meta file commits the output: written last, atomically
        fmt.write_json_atomic(
            out / fmt.meta_file(name), {**meta, "files": files, "rows": rows, "bytes": size}
        )

    def _declared(self, handle: str) -> None:
        if handle not in self.inputs:
            raise fmt.undeclared_error(handle, self.inputs)


def _transport() -> _FileTransport:
    run_dir = os.environ.get(fmt.ENV_RUN_DIR)
    if not run_dir:
        raise TnError(f"{fmt.ENV_RUN_DIR} is not set: tn works inside a run_code run")
    return _FileTransport(Path(run_dir))


def __getattr__(name: str) -> Any:
    if name == "inputs":  # per run: the kernel persists across runs, TN_RUN_DIR changes
        return _transport().inputs
    raise AttributeError(f"module 'telemetry_nerd.tn' has no attribute {name!r}")


def meta(handle: str) -> dict:
    """Meta of a declared input: representation, unit, step_ms, resolution_ms, start_ms,
    end_ms, source, expr, caveats, uncertainty, scheme (distributions), tables."""
    return _transport().input_meta(handle)


def dataset(handle: str, table: str = "rows", *, arrow: bool = False):
    """A declared input as a polars DataFrame (or a pyarrow Table with arrow=True).

    Memory-mapped, zero-copy into Arrow. Tables: "rows" (time series: ts_ms, series_id, avg,
    min, max, count[, lo, hi]; distribution: ts_ms, series_id, bucket_lo, bucket_hi, count),
    "series" (series_id, labels as JSON), "columns" (distribution: ts_ms, series_id, n)."""
    t = _transport().read_table(handle, table)
    if arrow:
        return t
    import polars as pl

    return pl.from_arrow(t, rechunk=False)


def _to_arrow(data: Any, what: str):
    import pyarrow as pa

    if isinstance(data, pa.Table):
        return data
    if hasattr(data, "to_arrow") and type(data).__module__.startswith("polars"):
        return data.to_arrow()
    raise TnError(
        f"{what} must be a polars DataFrame or a pyarrow Table, got {type(data).__name__}"
    )


def _next_name(tr: _FileTransport, prefix: str = "out") -> str:
    taken, i = tr.taken(), 1
    while f"{prefix}{i}" in taken:
        i += 1
    return f"{prefix}{i}"


def _claim(tr: _FileTransport, name: str | None, prefix: str = "out") -> str:
    if name is None:
        return _next_name(tr, prefix)
    fmt.check_name(name)
    if name in tr.taken():
        raise TnError(f"output {name!r} already exists in this run; pick another name")
    return name


def put(data: Any, meta: Mapping | None = None, *, columns: Any = None, **kw: Any) -> str:
    """Store a time series or distribution result; returns the output name.

    meta (or keyword args): like (an input handle to inherit step/range/unit/scheme from),
    representation (bucket_agg | sample | distribution), step_ms, resolution_ms, start_ms,
    end_ms, unit, description, labels (string columns naming each series; else a series_id
    column from an input, else one series), caveats, parents (inputs it depends on; default
    all declared), and the evidence rule: uncertainty={"method", "level", "kind"} with lo/hi
    columns, or exact=True for exact counts. Without either the output is stored but tagged
    no_uncertainty and cannot back a finding. `columns`: a distribution's (ts_ms, n) table."""
    tr = _transport()
    m = {**(meta or {}), **kw}
    m["name"] = _claim(tr, m.get("name"))
    norm = fmt.normalize_output_meta(m, tr.inputs, tr.input_meta)
    tables = {"rows": _to_arrow(data, "data")}
    if columns is not None:
        tables["columns"] = _to_arrow(columns, "columns")
    from telemetry_nerd.exchange.tables import normalize_output

    tables = normalize_output(norm, tables)
    tr.write_output(norm["name"], tables, {**norm, "version": fmt.VERSION, "kind": "dataset"})
    return norm["name"]


def put_fit(
    model: str,
    params: Mapping[str, Any],
    *,
    method: str,
    diagnostics: Mapping[str, Any],
    goodness: Mapping[str, Any] | None = None,
    level: float | None = None,
    prediction: Any = None,
    prediction_meta: Mapping | None = None,
    name: str | None = None,
    parents: list[str] | None = None,
    caveats: list[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> str:
    """Store a fit as an `estimate` dataset; returns its output name.

    params: {name: {"value": v, "interval": [lo, hi]}} (or {"value": n, "exact": True} for
    integral quantities), the shape of an evidence statistic; params without either are kept
    but tagged no_uncertainty. diagnostics (assumption checks) are mandatory. prediction: an
    optional time series (put() rules; give lo/hi + prediction_meta["uncertainty"] for bands)
    stored as output "<name>_prediction" with the fit as a parent. The fit's time range is
    its parents' unless start_ms/end_ms are given."""
    tr = _transport()
    name = _claim(tr, name, "fit")
    fit = fmt.normalize_fit(
        {"model": model, "params": params, "method": method, "diagnostics": diagnostics,
         "goodness": goodness, "level": level}
    )  # fmt: skip
    pm = {"parents": parents, "caveats": caveats, "start_ms": start_ms, "end_ms": end_ms}
    pm = {k: v for k, v in pm.items() if v is not None}
    norm_parents = fmt.normalize_output_meta(
        {"step_ms": 1, **pm}, tr.inputs, tr.input_meta
    )  # step is not used for a fit; this validates parents/caveats/range
    if prediction is not None:
        pname = _claim(tr, f"{name}_prediction")
        pmeta = {**(prediction_meta or {}), "name": pname}
        pnorm = fmt.normalize_output_meta(pmeta, tr.inputs, tr.input_meta)
        from telemetry_nerd.exchange.tables import normalize_output

        ptables = normalize_output(pnorm, {"rows": _to_arrow(prediction, "prediction")})
    meta = {
        "version": fmt.VERSION,
        "kind": "fit",
        "name": name,
        "representation": fmt.ESTIMATE,
        "parents": norm_parents["parents"],
        "caveats": norm_parents["caveats"],
        "start_ms": norm_parents["start_ms"],
        "end_ms": norm_parents["end_ms"],
        "fit": fit,
    }
    tr.write_output(name, {}, meta)
    if prediction is not None:
        tr.write_output(
            pname,
            ptables,
            {**pnorm, "version": fmt.VERSION, "kind": "dataset", "prediction_of": name},
        )
    return name
