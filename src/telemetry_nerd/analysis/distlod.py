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


def _mapping(lo, hi, scheme: BucketScheme, max_rows: int) -> tuple[Mapping, int]:
    if scheme.kind in ("classic", "custom"):
        return _edge_mapping(lo, hi, scheme.edges, max_rows)
    return None, 1  # native: Task 9; vmrange: Task 10


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
