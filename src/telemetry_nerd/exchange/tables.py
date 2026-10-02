"""Arrow IPC files of a run directory and the data checks on output tables (spec §5.2).

Imports only pyarrow (+ `fmt`); used by both the kernel (`tn.put`, lazily) and the daemon.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from telemetry_nerd.exchange.fmt import (
    DISTRIBUTION,
    TIME_SERIES,
    ExchangeError,
    check_columns,
    write_atomic,
)


def write_ipc(path: Path, table: pa.Table) -> int:
    """Arrow IPC *file* format (random access, mmap-able), uncompressed; returns bytes written."""

    def _write(f) -> None:
        with pa.ipc.new_file(f, table.schema) as w:
            w.write_table(table)

    write_atomic(path, _write)
    return path.stat().st_size


def read_ipc(path: Path) -> pa.Table:
    """Memory-mapped and zero-copy: the buffers point into the page cache. Files are only ever
    replaced by rename, so a mapped file is never truncated under a reader."""
    return pa.ipc.open_file(pa.memory_map(str(path), "r")).read_all()


def column_types(table: pa.Table) -> dict[str, str]:
    return {f.name: str(f.type) for f in table.schema}


def _cast(table: pa.Table, name: str, typ: pa.DataType) -> pa.Table:
    i = table.schema.get_field_index(name)
    try:
        col = table.column(i).cast(typ)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as e:
        raise ExchangeError(f"column {name!r} cannot be cast to {typ}: {e}") from e
    return table.set_column(i, pa.field(name, typ), col)


def _any(mask: pa.ChunkedArray | pa.Array) -> bool:
    return bool(pc.any(mask).as_py() or False)


def _nonintegral(col) -> bool:
    valid = pc.drop_null(col)
    return len(valid) > 0 and _any(pc.not_equal(valid, pc.floor(valid)))


def _series_key(meta: Mapping, table: pa.Table) -> list[str]:
    if "series_id" in table.column_names:
        return ["series_id"]
    return list(meta.get("labels") or [])


def duplicates(table: pa.Table, keys: list[str], what: str) -> None:
    if table.num_rows == 0:
        return
    counts = table.group_by(keys).aggregate([([], "count_all")])
    if pc.max(counts.column("count_all")).as_py() > 1:
        raise ExchangeError(f"duplicate rows for the same {' + '.join(keys)} in {what}")


def normalize_output(meta: Mapping, tables: Mapping[str, pa.Table]) -> dict[str, pa.Table]:
    """Check an output's tables against the column contract and the data rules; return them
    cast to canonical types. Raises ExchangeError with the reason."""
    rep = meta["representation"]
    expected = {"rows", "columns"} if rep == DISTRIBUTION else {"rows"}
    if "rows" not in tables or set(tables) - expected:
        raise ExchangeError(f"a {rep} output has tables {sorted(expected)}, got {sorted(tables)}")
    out: dict[str, pa.Table] = {}
    for name, table in tables.items():
        if not isinstance(table, pa.Table):
            raise ExchangeError(f"table {name!r} must be a pyarrow Table")
        check_columns(name, column_types(table), meta)
        for c in table.column_names:
            if c in ("ts_ms", "count") and not (rep == DISTRIBUTION and c == "count"):
                table = _cast(table, c, pa.int64())
            elif c == "series_id" or c in (meta.get("labels") or []):
                table = _cast(table, c, pa.string())
            else:
                table = _cast(table, c, pa.float64())
        if table.column("ts_ms").null_count:
            raise ExchangeError(f"{name}: ts_ms has nulls")
        duplicates(
            table,
            [*_series_key(meta, table), "ts_ms", *(["bucket_lo"] if rep == DISTRIBUTION and name == "rows" else [])],
            name,
        )  # fmt: skip
        out[name] = table
    rows = out["rows"]
    if rep in TIME_SERIES:
        _check_time_series(meta, rows)
    else:
        _check_distribution(meta, rows, out.get("columns"))
    return out


def _check_time_series(meta: Mapping, rows: pa.Table) -> None:
    avg = rows.column("avg")
    if meta.get("uncertainty") is not None:
        has = pc.is_valid(avg)
        lo, hi = rows.column("lo"), rows.column("hi")
        missing = pc.and_(has, pc.or_(pc.is_null(lo), pc.is_null(hi)))
        if _any(missing):
            n = pc.sum(missing.cast(pa.int64())).as_py()
            raise ExchangeError(
                f"uncertainty is declared but lo/hi are null in {n} rows with a value; "
                "drop those rows or leave uncertainty out (tagged no_uncertainty)"
            )
        if _any(pc.greater(lo, hi)):
            raise ExchangeError("lo > hi in some rows: an interval must be [lo, hi]")
        if _any(pc.or_(pc.is_nan(lo), pc.is_nan(hi))):
            raise ExchangeError("lo/hi contain NaN; use null for a missing bucket")
    if meta.get("exact") and _nonintegral(avg):
        raise ExchangeError(
            "exact=True is only for integral quantities such as counts; avg has fractions "
            "(declare an uncertainty instead)"
        )


def _check_distribution(meta: Mapping, rows: pa.Table, columns: pa.Table | None) -> None:
    for c in ("bucket_lo", "bucket_hi", "count"):
        if rows.column(c).null_count:
            raise ExchangeError(f"distribution rows: {c} has nulls")
    if _any(pc.greater_equal(rows.column("bucket_lo"), rows.column("bucket_hi"))):
        raise ExchangeError("distribution rows: bucket_lo must be < bucket_hi")
    count = rows.column("count")
    if _any(pc.or_(pc.less(count, 0), pc.is_nan(count))):
        raise ExchangeError("distribution rows: count must be >= 0")
    if meta.get("exact") and _nonintegral(count):
        raise ExchangeError("exact=True is only for integral counts; these counts have fractions")
    if columns is not None and columns.column("n").null_count:
        raise ExchangeError("distribution columns: n has nulls")
