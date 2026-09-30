# src/telemetry_nerd/analysis/resample.py
"""Re-aggregation that never erodes peaks or biases means."""

from __future__ import annotations

import math

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.series import BUCKET_SCHEMA
from telemetry_nerd.model.time import TimeRange


def rebucket(buckets: pa.Table, new_step_ms: int) -> pa.Table:
    """Merge buckets into coarser ones ending at multiples of new_step_ms."""
    if buckets.num_rows == 0:
        return buckets
    df = pl.from_arrow(buckets)
    k = new_step_ms
    total = pl.col("count").sum()
    weight = pl.col("count").filter(pl.col("avg").is_not_null()).sum()  # null avg carries no mean
    out = (
        df.with_columns(((pl.col("ts_ms") + k - 1) // k * k).alias("ts_ms"))
        .group_by(["series_id", "ts_ms"])
        .agg(
            pl.when(weight > 0)
            .then((pl.col("avg") * pl.col("count")).sum() / weight)
            .otherwise(None)
            .alias("avg"),
            pl.col("min").min(),
            pl.col("max").max(),
            total.alias("count"),
        )
        .sort(["series_id", "ts_ms"])
        .select(BUCKET_SCHEMA.names)
    )
    return out.to_arrow().cast(BUCKET_SCHEMA)


def lod(buckets: pa.Table, step_ms: int, rng: TimeRange, width_px: int) -> tuple[pa.Table, int]:
    """Reduce to roughly one bucket per pixel, preserving min/max envelopes."""
    n_buckets = (rng.end_ms - rng.start_ms) // step_ms + 1
    factor = max(1, math.ceil(n_buckets / max(1, width_px)))
    if factor == 1:
        return buckets, step_ms
    new_step = step_ms * factor
    return rebucket(buckets, new_step), new_step
