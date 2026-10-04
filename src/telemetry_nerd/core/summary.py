"""Compact dataset summaries for Claude: statistics and caveats, never raw series."""

from __future__ import annotations

import json
import math

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.analysis.quantiles import quantile_bucket
from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, Flag, State, grid
from telemetry_nerd.model.caveats import (
    CADENCE_OR_LOSS,
    NO_REASON,
    SUBQUERY_FILLS_GAPS,
    differing_intervals,
    failure_reasons,
    runs,
)
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
    """Where the data is unknown, and why: failed fetches plus UNKNOWN buckets; capped for Claude.
    Each span is [start, end, reasons]: the failures touching it, `subquery_fills_gaps` for an
    expression whose counts cannot be observed, `cadence_or_loss` for 0-sample buckets that keep a
    regular spacing (a slightly slower series interval or a periodic loss), else "source could not
    tell"."""
    ts: set[int] = set()  # straight from the dataset's failed fetches, independent of series
    for a, b, _reason in meta.failed_spans:
        ts.update(grid(max(a, meta.start_ms), min(b, meta.end_ms), meta.step_ms))
    filled: set[int] = set()
    cadence: set[int] = set()
    if df is not None and df.height:
        unknown = df.filter(pl.col("state") == int(State.UNKNOWN))
        ts.update(unknown["ts_ms"].to_list())
        flagged = lambda f: unknown.filter((pl.col("flags") & int(f)) != 0)["ts_ms"].to_list()
        filled.update(flagged(Flag.SOURCE_FILLED))
        cadence.update(flagged(Flag.CADENCE))
    spans = runs(sorted(ts), meta.step_ms)

    def why(a: int, b: int) -> str:
        inside = grid(a + 1, b, meta.step_ms)  # the bucket ends the span covers
        found = failure_reasons(meta.failed_spans, inside, meta.step_ms)
        if filled.intersection(inside):
            found = sorted({*found, SUBQUERY_FILLS_GAPS})
        if cadence.intersection(inside):
            found = sorted({*found, CADENCE_OR_LOSS})
        return "; ".join(found or [NO_REASON])

    return {
        "unknown_spans": [[iso(a), iso(b), why(a, b)] for a, b in spans[:UNKNOWN_SPANS_CAP]],
        "unknown_spans_more": max(0, len(spans) - UNKNOWN_SPANS_CAP),
    }


def _silent_members(meta: DatasetMeta, df: pl.DataFrame, labels: dict[str, dict], top: int) -> dict:
    """Members alive but without samples somewhere (EMPTY: no samples since T, source undetermined,
    principles 9 and 11), worst first, `top` named and the rest counted: the series list is ranked by
    value, so a silent member would otherwise be anywhere in it or past its end."""
    per = (
        df.filter(pl.col("state") == int(State.EMPTY))
        .group_by("series_id")
        .agg(pl.len().alias("n"))
        .sort("n", "series_id", descending=[True, False])
    )
    if not per.height:
        return {}
    return {
        "silent_members": [
            {
                "labels": labels.get(r["series_id"], {}),
                "silent_for": format_duration(r["n"] * meta.step_ms),
            }
            for r in per.head(top).to_dicts()
        ],
        "silent_more": max(0, per.height - top),
    }


def _coverage(
    meta: DatasetMeta, result: FetchResult, states: pa.Table | None
) -> tuple[dict[str, dict], dict, pl.DataFrame]:
    """Per series: observed/expected share, total missing, longest gap; dataset unknown spans.

    Everything comes from the bucket_state (the bundle's when given), never from re-judging
    summed counts."""
    if states is None:
        states = STATE_SCHEMA.empty_table() if meta.code_node else derive_states(meta, result)
    df = pl.from_arrow(states)
    bad = [int(State.EMPTY), int(State.PARTIAL)]
    out: dict[str, dict] = {}
    for (sid,), g in df.group_by("series_id"):
        # UNKNOWN is neither present nor missing: out of the share and of the gaps (it is
        # reported as unknown_spans, with its reason)
        alive = g.filter(~pl.col("state").is_in([int(State.ABSENT), int(State.UNKNOWN)]))
        exp = alive["expected"].sum()
        gaps = runs(alive.filter(pl.col("state").is_in(bad))["ts_ms"].to_list(), meta.step_ms)
        longest = max((b - a for a, b in gaps), default=0)
        if ((g["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():
            # coverage cannot be told from this expression: unknown, not zero
            out[sid] = {"pct": None, "missing": None, "longest_gap": None}
            continue
        unjudged = not exp and (g["state"] == int(State.UNKNOWN)).any()  # nothing could tell
        out[sid] = {
            "pct": None
            if unjudged
            else _round(min(1.0, alive["observed"].sum() / exp) if exp else 0.0),
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


def produced_by(meta: DatasetMeta) -> dict:
    """What a code output's summary adds: the producer, its inputs, declared unit/uncertainty."""
    if meta.code_node is None:
        return {}
    p = meta.producer or {}
    return {
        "produced_by": {"code_node": p.get("node"), "output": p.get("output"),
                        "parents": list(meta.parents)},
        "unit": meta.unit,
        "uncertainty": meta.uncertainty,
    }  # fmt: skip


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
    caveats += [c for c in meta.source_caveats if c not in caveats]
    base = {
        "dataset": meta.id,
        "expr": meta.expr,
        "range": [iso(meta.start_ms), iso(meta.end_ms)],
        "step": format_duration(meta.step_ms),
        "representation": meta.representation,
        **produced_by(meta),
    }
    if meta.representation == "quantile":
        base |= {"quantile": meta.quantile, "n_min": meta.n_min}
    # even with nothing returned, failed fetches make the data untrusted (not merely empty)
    labels = _labels_by_id(result.series)
    coverage, unknown, df = _coverage(meta, result, states)
    base |= unknown | _silent_members(meta, df, labels, top)
    _coverage_caveats(meta, df, unknown, caveats)
    if result.buckets.num_rows == 0:
        return _empty(base, caveats)
    # The grid is inclusive of both start and end (query_range and cache reads are BETWEEN).
    expected = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    if meta.representation == "quantile":
        return _summarize_quantile(meta, result, base, caveats, expected, top, coverage)
    # NaN / Inf (a positive observation: the source reported a non-finite value) and null (no
    # value for a bucket that has samples: absence, cause unknown) both carry a count but no
    # usable value; they must not bias the mean: weight only buckets that have an avg.
    raw = pl.from_arrow(result.buckets)
    df = raw.with_columns(pl.col("avg", "min", "max").fill_nan(None))
    # a null count is unknown (a code output that did not give it), never zero: such a bucket has
    # data when it has a value, and a series with unknown counts has the plain mean of its buckets
    known = pl.col("count").is_not_null()
    # (a code output's min/max are optional: missing there means not given, not missing data)
    cols = ("avg",) if meta.code_node else ("avg", "min", "max")
    sampled = raw.filter(pl.col("count") > 0)
    if sampled.filter(pl.any_horizontal(pl.col(*cols).is_nan())).height:
        caveats.append("non_finite")
    if sampled.filter(pl.any_horizontal(pl.col(*cols).is_null())).height:
        caveats.append("no_value")
    total = pl.col("count").filter(pl.col("avg").is_not_null()).sum()
    weighted = (
        pl.when(total > 0).then((pl.col("avg") * pl.col("count")).sum() / total).otherwise(None)
    )
    if df["count"].null_count() and "counts_unknown" not in caveats:
        caveats.append("counts_unknown")
    per = (
        df.group_by("series_id")
        .agg(
            pl.col("min").min().alias("min"),
            pl.col("max").max().alias("max"),
            pl.when(known.all()).then(weighted).otherwise(pl.col("avg").mean()).alias("mean"),
            # a bucket kept with count 0 holds a spilled scrape's own value (uup): data, no gap
            ((pl.col("count") > 0) | pl.col("avg").is_not_null()).sum().alias("with_data"),
        )
        .with_columns(
            pl.max_horizontal(pl.lit(expected) - pl.col("with_data"), pl.lit(0)).alias("gaps")
        )
        # ties (equal max, all-null) by id: group_by order is random per process (n3sv)
        .sort("max", "series_id", descending=[True, False], nulls_last=True)
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
    """Percentiles are reported per bucket with their n; never averaged (docs/principles.md, principles 4 and 10)."""
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
        .sort(
            "n_total" if meta.n_min is not None else "buckets",
            "series_id",
            descending=[True, False],
        )
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
        "counts": (
            "as produced by the code; additive over time and adjacent buckets"
            if meta.code_node
            else "increase() per step; additive over time and adjacent buckets"
        ),
        **produced_by(meta),
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
            # the latest of the busiest columns: rows owe no order (n3sv)
            pl.col("ts_ms").sort_by("n", "ts_ms").last().alias("busiest_ts"),
            pl.col("n").max().alias("busiest_n"),
        )
        .with_columns(
            pl.max_horizontal(pl.lit(expected) - pl.col("columns"), pl.lit(0)).alias(
                "missing_columns"
            )
        )
        .sort("n_total", "series_id", descending=[True, False])
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
