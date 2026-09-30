"""Compact dataset summaries for Claude: statistics and caveats, never raw series."""

from __future__ import annotations

import json

import polars as pl

from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import format_duration, iso


def _round(x: float | None) -> float | None:
    return None if x is None else float(f"{x:.4g}")


def summarize(
    meta: DatasetMeta, result: FetchResult, *, now_ms: int, settle_ms: int, top: int = 5
) -> dict:
    caveats: list[str] = []
    if meta.step_ms < meta.resolution_ms:
        caveats.append("fake_resolution")
    if meta.end_ms > now_ms - settle_ms:
        caveats.append("settling")
    base = {
        "dataset": meta.id,
        "expr": meta.expr,
        "range": [iso(meta.start_ms), iso(meta.end_ms)],
        "step": format_duration(meta.step_ms),
        "representation": meta.representation,
    }
    if result.buckets.num_rows == 0:
        return {
            **base,
            "series_count": 0,
            "series": [],
            "more_series": 0,
            "caveats": ["empty", *caveats],
        }
    labels = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
    # The grid is inclusive of both start and end (query_range and cache reads are BETWEEN).
    expected = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    total = pl.col("count").sum()
    per = (
        pl.from_arrow(result.buckets)
        .group_by("series_id")
        .agg(
            pl.col("min").min().alias("min"),
            pl.col("max").max().alias("max"),
            pl.when(total > 0)
            .then((pl.col("avg") * pl.col("count")).sum() / total)
            .otherwise(None)
            .alias("mean"),
            (pl.col("count") > 0).sum().alias("with_data"),
        )
        .with_columns(
            pl.max_horizontal(pl.lit(expected) - pl.col("with_data"), pl.lit(0)).alias("gaps")
        )
        .sort("max", descending=True, nulls_last=True)
    )
    if per["gaps"].sum() > 0:
        caveats.insert(0, "gaps")
    series = [
        {
            "labels": labels.get(r["series_id"], {}),
            "min": _round(r["min"]),
            "max": _round(r["max"]),
            "mean": _round(r["mean"]),
            "gaps": int(r["gaps"]),
        }
        for r in per.head(top).to_dicts()
    ]
    return {
        **base,
        "series_count": per.height,
        "series": series,
        "more_series": max(0, per.height - top),
        "caveats": caveats,
    }
