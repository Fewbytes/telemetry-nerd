"""Marginal histograms: current vs reference value distribution (spec §6.4; bead 4ok.6).

Two bases, always labelled: observations from a histogram (source buckets, additive) or
the per-step values drawn on a time panel (scrape-derived samples, NOT requests)."""

from __future__ import annotations

import bisect
import itertools

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.distlod import window_histogram

SAMPLE_BINS = 96
SAMPLE_N_MIN = 20  # fewer step values than this: the shape is noise (faded, caveat)


def step_values(
    buckets: pa.Table, representation: str, n_min: int | None
) -> tuple[list[float], int, int]:
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    df = df.filter(pl.col("avg").is_not_null() & pl.col("avg").is_finite())
    excluded = 0
    if representation == "quantile" and n_min is not None:
        ok = df["count"].fill_null(0) >= n_min
        excluded, df = int((~ok).sum()), df.filter(ok)
    return df["avg"].to_list(), excluded, df["series_id"].n_unique()


def sample_bins(
    cur: list[float], ref: list[float], bins: int = SAMPLE_BINS
) -> list[tuple[float, float]]:
    vals = cur + ref
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if lo == hi:
        pad = abs(lo) * 0.01 or 0.5
        return [(lo - pad, hi + pad)]
    if lo > 0 and hi / lo > 100:
        r = (hi / lo) ** (1 / bins)
        edges = [lo * r**i for i in range(bins + 1)]
    else:
        w = (hi - lo) / bins
        edges = [lo + w * i for i in range(bins + 1)]
    edges[0], edges[-1] = lo, hi
    return list(itertools.pairwise(edges))


def histogram_of(values: list[float], edges: list[tuple[float, float]]) -> list[int]:
    his = [h for _, h in edges]
    c = [0] * len(edges)
    for v in values:
        c[min(bisect.bisect_left(his, v), len(edges) - 1)] += 1
    return c


def pooled_window(
    rows: pl.DataFrame, cols: pl.DataFrame, step_ms: int, start_ms: int, end_ms: int
) -> dict | None:
    """All series summed (counts are additive): the distribution of every observation."""
    k = cols["series_id"].n_unique()
    rows = rows.with_columns(pl.lit("all").alias("series_id"))
    cols = (
        cols.group_by("ts_ms").agg(pl.col("n").sum()).with_columns(pl.lit("all").alias("series_id"))
    )
    w = window_histogram(rows, cols, step_ms, start_ms, end_ms).get("all")
    return None if w is None else {**w, "series": k}
