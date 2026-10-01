"""Attach the number of observations behind each per-bucket quantile value."""

from __future__ import annotations

import polars as pl

from telemetry_nerd.model.series import BUCKET_SCHEMA, FetchResult


def attach_counts(values: FetchResult, counts: FetchResult) -> FetchResult:
    v = pl.from_arrow(values.buckets).drop("count")
    c = pl.from_arrow(counts.buckets).select(
        "series_id", "ts_ms", pl.col("avg").fill_nan(None).alias("n")
    )
    joined = (
        v.join(c, on=["series_id", "ts_ms"], how="left")
        .with_columns(pl.col("n").round(0).fill_null(0).cast(pl.Int64).alias("count"))
        .select(BUCKET_SCHEMA.names)
        .sort(["series_id", "ts_ms"])
    )
    return FetchResult(joined.to_arrow().cast(BUCKET_SCHEMA), values.series, partial=values.partial)
