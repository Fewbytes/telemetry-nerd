"""Compact dataset summaries for Claude: statistics and caveats, never raw series."""

from __future__ import annotations

import json
import math

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.analysis.quantiles import quantile_bucket
from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.bucket_state import Flag, State, grid
from telemetry_nerd.model.caveats import differing_intervals, runs
from telemetry_nerd.model.companions import derive_states
from telemetry_nerd.model.distribution import DistResult
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import format_duration, iso


def _round(x: float | None) -> float | None:
    return None if x is None else float(f"{x:.4g}")


def _labels_by_id(series_table) -> dict[str, dict]:
    return {r["series_id"]: json.loads(r["labels"]) for r in series_table.to_pylist()}


def _empty(base: dict, caveats: list[str]) -> dict:
    return {
        **base,
        "series_count": 0,
        "series": [],
        "more_series": 0,
        "caveats": ["empty", *caveats],
    }


UNKNOWN_SPANS_CAP = 10


def _unknown_spans(meta: DatasetMeta, df: pl.DataFrame | None) -> dict:
    """Where the data is unknown: failed fetches plus UNKNOWN buckets; capped for Claude."""
    ts: list[int] = []  # straight from the dataset's failed fetches, independent of series
    for a, b, _reason in meta.failed_spans:
        ts += grid(max(a, meta.start_ms), min(b, meta.end_ms), meta.step_ms)
    if df is not None and df.height:
        ts += df.filter(pl.col("state") == int(State.UNKNOWN))["ts_ms"].to_list()
    spans = runs(sorted(set(ts)), meta.step_ms)
    return {
        "unknown_spans": [[iso(a), iso(b)] for a, b in spans[:UNKNOWN_SPANS_CAP]],
        "unknown_spans_more": max(0, len(spans) - UNKNOWN_SPANS_CAP),
    }


def _coverage(
    meta: DatasetMeta, result: FetchResult, states: pa.Table | None
) -> tuple[dict[str, dict], dict, pl.DataFrame]:
    """Per series: observed/expected share, total missing, longest gap; dataset unknown spans.

    Everything comes from the bucket_state (the bundle's when given), never from re-judging
    summed counts."""
    if states is None:
        states = derive_states(meta, result)
    df = pl.from_arrow(states)
    bad = [int(State.EMPTY), int(State.PARTIAL), int(State.UNKNOWN)]
    out: dict[str, dict] = {}
    for (sid,), g in df.group_by("series_id"):
        alive = g.filter(pl.col("state") != int(State.ABSENT))
        exp = alive["expected"].sum()
        gaps = runs(alive.filter(pl.col("state").is_in(bad))["ts_ms"].to_list(), meta.step_ms)
        longest = max((b - a for a, b in gaps), default=0)
        if ((g["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():
            # coverage cannot be told from this expression: unknown, not zero
            out[sid] = {"pct": None, "missing": None, "longest_gap": None}
            continue
        out[sid] = {
            "pct": _round(min(1.0, alive["observed"].sum() / exp) if exp else 0.0),
            "missing": format_duration(sum(b - a for a, b in gaps)) if gaps else "0s",
            "longest_gap": format_duration(longest) if longest else None,
        }
    return out, _unknown_spans(meta, df), df


def _coverage_caveats(
    meta: DatasetMeta, df: pl.DataFrame, unknown: dict, caveats: list[str]
) -> None:
    """missing_data: some alive bucket is PARTIAL or EMPTY; untrusted_data: anything UNKNOWN."""
    if unknown["unknown_spans"]:
        caveats.append("untrusted_data")
    if ((df["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():  # the expression itself hides coverage
        caveats.append("unobservable_counts")
    post_gap = ((df["flags"] & int(Flag.POST_GAP)) != 0) & (df["state"] != int(State.UNKNOWN))
    if post_gap.any():
        caveats.append("post_gap_spike")
    if df.height and df["state"].is_in([int(State.PARTIAL), int(State.EMPTY)]).any():
        caveats.append("missing_data")
    if meta.representation != "quantile":  # presence mode has no sample counts to judge
        if differing_intervals(df.to_arrow(), meta.step_ms, meta.resolution_ms):
            caveats.append("interval_differs")
        if ((df["flags"] & int(Flag.INTERVAL_CHANGE)) != 0).any():
            caveats.append("interval_change")


def _base_caveats(meta: DatasetMeta, now_ms: int, settle_ms: int) -> list[str]:
    caveats: list[str] = []
    if meta.step_ms < meta.resolution_ms:
        caveats.append("fake_resolution")
    if meta.end_ms > now_ms - settle_ms:
        caveats.append("settling")
    return caveats


def summarize(
    meta: DatasetMeta,
    result: FetchResult,
    *,
    now_ms: int,
    settle_ms: int,
    top: int = 5,
    states: pa.Table | None = None,
) -> dict:
    caveats = _base_caveats(meta, now_ms, settle_ms)
    if meta.partial > 0:
        caveats.append("partial")
    base = {
        "dataset": meta.id,
        "expr": meta.expr,
        "range": [iso(meta.start_ms), iso(meta.end_ms)],
        "step": format_duration(meta.step_ms),
        "representation": meta.representation,
    }
    if meta.representation == "quantile":
        base |= {"quantile": meta.quantile, "n_min": meta.n_min}
    # even with nothing returned, failed fetches make the data untrusted (not merely empty)
    labels = _labels_by_id(result.series)
    coverage, unknown, df = _coverage(meta, result, states)
    base |= unknown
    _coverage_caveats(meta, df, unknown, caveats)
    if result.buckets.num_rows == 0:
        return _empty(base, caveats)
    # The grid is inclusive of both start and end (query_range and cache reads are BETWEEN).
    expected = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    if meta.representation == "quantile":
        return _summarize_quantile(meta, result, base, caveats, expected, top, coverage)
    # Non-finite values (null, or NaN from a careless source) carry a count but no value.
    # They must not bias the mean: weight only buckets that have an avg.
    df = pl.from_arrow(result.buckets).with_columns(pl.col("avg", "min", "max").fill_nan(None))
    if df.filter(
        (pl.col("count") > 0) & pl.any_horizontal(pl.col("avg", "min", "max").is_null())
    ).height:
        caveats.append("non_finite")
    total = pl.col("count").filter(pl.col("avg").is_not_null()).sum()
    per = (
        df.group_by("series_id")
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
            "coverage": coverage.get(r["series_id"]),
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


def _summarize_quantile(
    meta: DatasetMeta,
    result: FetchResult,
    base: dict,
    caveats: list[str],
    expected: int,
    top: int,
    coverage: dict[str, dict],
) -> dict:
    """Percentiles are reported per bucket with their n; never averaged (spec §1.2)."""
    labels = _labels_by_id(result.series)
    df = pl.from_arrow(result.buckets).with_columns(pl.col("avg").fill_nan(None))
    # a value with a missing count (0) is still a value: it is "not meaningful", not a gap
    has_value = pl.col("avg").is_not_null()
    meaningful = has_value & (pl.col("count") >= (meta.n_min or 0))
    per = (
        df.group_by("series_id")
        .agg(
            pl.col("avg").filter(meaningful).min().alias("min"),
            pl.col("avg").filter(meaningful).max().alias("max"),
            pl.col("count").sum().alias("n_total"),
            has_value.sum().alias("buckets"),
            meaningful.sum().alias("meaningful_buckets"),
        )
        .with_columns(
            pl.max_horizontal(pl.lit(expected) - pl.col("buckets"), pl.lit(0)).alias("gaps")
        )
        .sort("n_total" if meta.n_min is not None else "buckets", descending=True)
    )
    if meta.n_min is None:
        caveats.append("n_unknown")
    elif (per["meaningful_buckets"] < per["buckets"]).any():
        caveats.append("low_count")
    if per["gaps"].sum() > 0:
        caveats.insert(0, "gaps")
    series = [
        {
            "labels": labels.get(r["series_id"], {}),
            "min": _round(r["min"]),
            "max": _round(r["max"]),
            "mean": None,
            # without a derived n the count column is a placeholder: never report it as n
            "n_total": int(r["n_total"]) if meta.n_min is not None else None,
            "buckets": int(r["buckets"]),
            "meaningful_buckets": int(r["meaningful_buckets"]) if meta.n_min is not None else None,
            "gaps": int(r["gaps"]),
            "coverage": coverage.get(r["series_id"]),
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


QUANTILE_BOUNDS = (0.5, 0.9, 0.99)


def _edge(x: float) -> float | str:
    if math.isinf(x):
        return "+Inf" if x > 0 else "-Inf"
    return _round(x)


def _quantile_bucket(buckets: list[tuple[float, float, float]], q: float) -> list | None:
    """The source bucket that contains quantile q: honest bounds, never interpolated."""
    b = quantile_bucket(buckets, q)
    return None if b is None else [_edge(b[0]), _edge(b[1])]


def summarize_distribution(
    meta: DatasetMeta, dist: DistResult, *, now_ms: int, settle_ms: int, top: int = 5
) -> dict:
    """Counts per bucket per step: n, coverage, low-n columns, and where the quantiles lie
    (bucket bounds, only where n is enough). Never a percentile value."""
    caveats = _base_caveats(meta, now_ms, settle_ms)
    caveats += [c for c in meta.source_caveats if c not in caveats]
    base = {
        "dataset": meta.id,
        "expr": meta.expr,
        "range": [iso(meta.start_ms), iso(meta.end_ms)],
        "step": format_duration(meta.step_ms),
        "representation": meta.representation,
        "buckets": (meta.scheme or {}).get("description", "no buckets"),
        "n_min": meta.n_min,
        "counts": "increase() per step; additive over time and adjacent buckets",
    }
    if dist.columns.num_rows == 0:
        return _empty(base, caveats)
    expected = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    n_min = meta.n_min or 0
    labels = _labels_by_id(dist.series)
    per = (
        pl.from_arrow(dist.columns)
        .group_by("series_id")
        .agg(
            pl.col("n").sum().alias("n_total"),
            pl.len().alias("columns"),
            (pl.col("n") == 0).sum().alias("zero_columns"),
            ((pl.col("n") > 0) & (pl.col("n") < n_min)).sum().alias("low_n_columns"),
            pl.col("ts_ms").sort_by("n").last().alias("busiest_ts"),
            pl.col("n").max().alias("busiest_n"),
        )
        .with_columns(
            pl.max_horizontal(pl.lit(expected) - pl.col("columns"), pl.lit(0)).alias(
                "missing_columns"
            )
        )
        .sort("n_total", descending=True)
    )
    merged: dict[str, list[tuple[float, float, float]]] = {}
    for r in (
        pl.from_arrow(dist.rows)
        .group_by("series_id", "bucket_lo", "bucket_hi")
        .agg(pl.col("count").sum())
        .iter_rows(named=True)
    ):
        merged.setdefault(r["series_id"], []).append((r["bucket_lo"], r["bucket_hi"], r["count"]))
    if per["missing_columns"].sum() > 0:
        caveats.insert(0, "gaps")
    if per["low_n_columns"].sum() > 0:
        caveats.append("low_count")
    if any(hi == math.inf for b in merged.values() for _, hi, _ in b):
        caveats.append("overflow")
    series = []
    for r in per.head(top).to_dicts():
        buckets = merged.get(r["series_id"], [])
        series.append(
            {
                "labels": labels.get(r["series_id"], {}),
                "n_total": _round(r["n_total"]),
                "columns": int(r["columns"]),
                "zero_columns": int(r["zero_columns"]),
                "missing_columns": int(r["missing_columns"]),
                "low_n_columns": int(r["low_n_columns"]),
                "busiest": {"at": iso(r["busiest_ts"]), "n": _round(r["busiest_n"])},
                "quantile_buckets": {
                    f"p{q * 100:g}": _quantile_bucket(buckets, q)
                    for q in QUANTILE_BOUNDS
                    if r["n_total"] >= min_samples(q)
                },
            }
        )
    return {
        **base,
        "series_count": per.height,
        "series": series,
        "more_series": max(0, per.height - top),
        "caveats": caveats,
    }
