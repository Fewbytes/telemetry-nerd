"""Level of detail for distributions (spec §6.5).

Counts are additive, so summing columns in time and merging adjacent value buckets is
exact. Merged buckets are unions of whole source buckets: never finer than the source
and never split.
"""

from __future__ import annotations

import bisect
import math

import polars as pl

from telemetry_nerd.model.distribution import BucketScheme

PX_PER_COLUMN = 2
PX_PER_ROW = 4
PX_PER_CELL = PX_PER_COLUMN * PX_PER_ROW
FACET_HEIGHT_SINGLE = 260
FACET_HEIGHT_MULTI = 140
_KEYS = ["series_id", "ts_ms", "bucket_lo", "bucket_hi"]

Mapping = list[tuple[float, float]] | None


def rebucket_time(
    rows: pl.DataFrame, cols: pl.DataFrame, new_step_ms: int
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Columns end at multiples of new_step_ms (as resample.rebucket); counts, n and cover
    (number of source columns with data) are summed."""
    k = new_step_ms
    end = ((pl.col("ts_ms") + k - 1) // k * k).alias("ts_ms")
    rows = rows.with_columns(end).group_by(_KEYS).agg(pl.col("count").sum()).sort(_KEYS)
    cols = (
        cols.with_columns(end)
        .group_by(["series_id", "ts_ms"])
        .agg(pl.col("n").sum(), pl.col("cover").sum())
        .sort(["series_id", "ts_ms"])
    )
    return rows, cols


def _edge_mapping(lo, hi, edges: tuple[float, ...], max_rows: int) -> tuple[Mapping, int]:
    n_finite = len(edges) - 1
    if n_finite < 1:
        return None, 1
    m = math.ceil(n_finite / max_rows)
    if m <= 1:
        return None, 1
    kept = list(edges[::m])
    if kept[-1] != edges[-1]:
        kept.append(edges[-1])
    out = []
    for low, high in zip(lo, hi, strict=True):
        if not (math.isfinite(low) and math.isfinite(high)):
            out.append((low, high))
            continue
        j = bisect.bisect_left(kept, high - 1e-12 * abs(high))  # first kept edge >= high
        out.append((kept[j - 1], kept[j]))
    return out, m


def _native_mapping(lo, hi, max_rows: int) -> tuple[Mapping, int]:
    """Coarsen to a lower schema: merge 2^k adjacent buckets on the coarsest schema's grid,
    so buckets of different schemas in one series nest. Zero, negative and open buckets stay."""
    spec: list[int | None] = []
    for low, high in zip(lo, hi, strict=True):
        ok = low > 0 and math.isfinite(high) and high > low
        spec.append(round(-math.log2(math.log2(high / low))) if ok else None)
    known = [s for s in spec if s is not None]
    if not known:
        return None, 1
    fine, coarse = max(known), min(known)
    idx = [
        round(math.log2(high) * 2**fine) if s is not None else None
        for high, s in zip(hi, spec, strict=True)
    ]
    finite = [i for i in idx if i is not None]
    unit = 2 ** (fine - coarse)  # fine indices per coarsest bucket
    span = (max(finite) - min(finite)) // unit + 1
    m = 1 << max(0, math.ceil(math.log2(max(1.0, span / max_rows))))
    if m == 1:
        return None, 1
    width = m * unit
    out = []
    for low, high, i in zip(lo, hi, idx, strict=True):
        if i is None:
            out.append((low, high))
        else:
            u = -(-i // width) * width
            out.append((2.0 ** ((u - width) / 2**fine), 2.0 ** (u / 2**fine)))
    return out, m


def _vm_mapping(lo, hi, per_decade: int, max_rows: int) -> tuple[Mapping, int]:
    idx = [
        round(math.log10(high) * per_decade) if low > 0 and math.isfinite(high) else None
        for low, high in zip(lo, hi, strict=True)
    ]
    finite = [i for i in idx if i is not None]
    if not finite:
        return None, 1
    m = math.ceil((max(finite) - min(finite) + 1) / max_rows)
    if m <= 1:
        return None, 1
    out = []
    for low, high, i in zip(lo, hi, idx, strict=True):
        if i is None:
            out.append((low, high))
        else:
            u = -(-i // m) * m
            out.append((10 ** ((u - m) / per_decade), 10 ** (u / per_decade)))
    return out, m


def _mapping(lo, hi, scheme: BucketScheme, max_rows: int) -> tuple[Mapping, int]:
    if scheme.kind in ("classic", "custom"):
        return _edge_mapping(lo, hi, scheme.edges, max_rows)
    if scheme.kind == "native":
        return _native_mapping(lo, hi, max_rows)
    if scheme.kind == "vmrange" and scheme.per_decade:
        return _vm_mapping(lo, hi, scheme.per_decade, max_rows)
    return None, 1


def merge_values(
    rows: pl.DataFrame, scheme: BucketScheme, max_rows: int
) -> tuple[pl.DataFrame, int]:
    """Merge adjacent value buckets so each series has at most ~max_rows finite buckets."""
    if rows.height == 0 or max_rows < 1:
        return rows, 1
    parts, factor, changed = [], 1, False
    for part in rows.partition_by("series_id", maintain_order=True):
        mapped, m = _mapping(
            part["bucket_lo"].to_list(), part["bucket_hi"].to_list(), scheme, max_rows
        )
        factor = max(factor, m)
        if mapped is not None:
            changed = True
            part = part.with_columns(
                pl.Series("bucket_lo", [a for a, _ in mapped], dtype=pl.Float64),
                pl.Series("bucket_hi", [b for _, b in mapped], dtype=pl.Float64),
            )
        parts.append(part)
    if not changed:
        return rows, 1
    out = pl.concat(parts).group_by(_KEYS).agg(pl.col("count").sum()).sort(_KEYS)
    return out.select(rows.columns), factor


HIST_PX_PER_BAR = 3


def window_histogram(
    rows: pl.DataFrame, cols: pl.DataFrame, step_ms: int, start_ms: int, end_ms: int
) -> dict[str, dict]:
    """Sum whole columns (ts - step, ts] that overlap [start, end], per series. The window
    snaps outward to those columns; n and counts are sums (additive)."""
    inside = (pl.col("ts_ms") > start_ms) & (pl.col("ts_ms") - step_ms < end_ms)
    c = (
        cols.filter(inside)
        .group_by("series_id")
        .agg(pl.col("n").sum(), pl.len().alias("columns"),
             pl.col("ts_ms").min().alias("first"), pl.col("ts_ms").max().alias("last"))
    )  # fmt: skip
    r = (
        rows.filter(inside)
        .group_by("series_id", "bucket_lo", "bucket_hi")
        .agg(pl.col("count").sum())
        .sort("series_id", "bucket_hi", "bucket_lo")
    )
    out: dict[str, dict] = {}
    for row in c.iter_rows(named=True):
        b = r.filter(pl.col("series_id") == row["series_id"])
        out[row["series_id"]] = {
            "start_ms": row["first"] - step_ms,
            "end_ms": row["last"],
            "n": row["n"],
            "columns": row["columns"],
            "lo": b["bucket_lo"].to_list(),
            "hi": b["bucket_hi"].to_list(),
            "c": b["count"].to_list(),
        }
    return out
