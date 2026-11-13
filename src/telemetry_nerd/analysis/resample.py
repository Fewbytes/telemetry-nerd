# src/telemetry_nerd/analysis/resample.py
"""Re-aggregation that never erodes peaks or biases means."""

from __future__ import annotations

import math

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.mergekind import UNMERGEABLE, MergeKind
from telemetry_nerd.model.series import BUCKET_SCHEMA
from telemetry_nerd.model.time import TimeRange


def rebucket(buckets: pa.Table, new_step_ms: int, kind: MergeKind = "sample_mean") -> pa.Table:
    """Merge buckets into coarser ones ending at multiples of new_step_ms, by the rule of `kind`:
    sample mean (count-weighted), time mean (`time_mean`, `sum`: equal weight, every query bucket
    spans the same time), or the extreme of the envelope (`max`, `min`). The envelope is kept
    for every kind. Raises for kinds that cannot be merged (ratio, quantile, unknown)."""
    if kind in UNMERGEABLE:
        raise ValueError(f"{kind} buckets cannot be merged: keep the dataset's own step")
    if buckets.num_rows == 0:
        return buckets
    raw = pl.from_arrow(buckets)
    # NaN (non-finite, a positive observation) never poisons a merged value; a merged bucket
    # with no finite value stays NaN where any member was NaN, null (no value) otherwise
    df = raw.with_columns(pl.col("avg", "min", "max").fill_nan(None))
    nan_cell = pl.any_horizontal(pl.col("avg", "min", "max").is_nan()).alias("_nan")
    df = df.with_columns(raw.select(nan_cell))
    k = new_step_ms
    nan_in = pl.col("_nan").any()
    nan = pl.lit(math.nan)
    total = pl.col("count").sum()
    has_value = pl.col("avg").is_not_null().any()
    weight = pl.col("count").filter(pl.col("avg").is_not_null()).sum()  # null avg carries no mean
    # a null count is unknown (code outputs), never zero: where any is unknown the merged bucket
    # is the plain mean of its values and its count stays unknown
    known = pl.col("count").is_not_null().all()
    by_count = pl.when(~known).then(pl.col("avg").mean()).when(weight > 0)
    by_count = by_count.then((pl.col("avg") * pl.col("count")).sum() / weight)
    merged_avg = {
        "sample_mean": by_count,
        "time_mean": pl.when(has_value).then(pl.col("avg").mean()),
        "sum": pl.when(has_value).then(pl.col("avg").mean()),
        "max": pl.when(has_value).then(pl.col("max").max()),
        "min": pl.when(has_value).then(pl.col("min").min()),
    }[kind]
    out = (
        df.with_columns(((pl.col("ts_ms") + k - 1) // k * k).alias("ts_ms"))
        .group_by(["series_id", "ts_ms"])
        .agg(
            merged_avg.when(nan_in).then(nan).otherwise(None).alias("avg"),
            *(  # a bucket with a value but no min/max (not given by code): the envelope is unknown
                pl.when((pl.col(c).is_null() & pl.col("avg").is_not_null()).any())
                .then(None)
                .when(pl.col(c).is_null().all() & nan_in)
                .then(nan)
                .otherwise(getattr(pl.col(c), c)())
                .alias(c)
                for c in ("min", "max")
            ),
            pl.when(known).then(total).otherwise(None).alias("count"),
        )
        .sort(["series_id", "ts_ms"])
        .select(BUCKET_SCHEMA.names)
    )
    return out.to_arrow().cast(BUCKET_SCHEMA)


def lod(
    buckets: pa.Table,
    step_ms: int,
    rng: TimeRange,
    width_px: int,
    kind: MergeKind = "sample_mean",
) -> tuple[pa.Table, int]:
    """Reduce to roughly one bucket per pixel, preserving min/max envelopes. Buckets of a kind
    that cannot be merged are returned at their own step."""
    if kind in UNMERGEABLE:
        return buckets, step_ms
    n_buckets = (rng.end_ms - rng.start_ms) // step_ms + 1
    factor = max(1, math.ceil(n_buckets / max(1, width_px)))
    if factor == 1:
        return buckets, step_ms
    new_step = step_ms * factor
    return rebucket(buckets, new_step), new_step
