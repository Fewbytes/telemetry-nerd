"""bucket_state: per series and bucket, how much of the expected data arrived and whether it can
be trusted (spec 2026-10-02 §5). Computed on read from bucket counts; never stored."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import IntEnum, IntFlag
from typing import Literal

import polars as pl
import pyarrow as pa


class State(IntEnum):
    OK = 0
    PARTIAL = 1
    EMPTY = 2  # alive, no samples (also: silent after the last sample; spec decision 1)
    ABSENT = 3  # before the first sample in the window: not alive yet
    UNKNOWN = 4  # fetch failed / source cannot tell


class Flag(IntFlag):
    NONE = 0
    RESET = 1
    INTERVAL_CHANGE = 2
    STALE_MARKER = 4
    SOURCE_FILLED = 8  # the expression's counts are subquery evaluations, not samples (UNKNOWN)
    POST_GAP = 16  # value right after a gap is computed from the sample before it (OK/PARTIAL)


PARTIAL_RATIO = 0.9
CHANGE_RATIO = 2.0  # a series' sample rate halves or doubles within the window
CHANGE_MIN_BUCKETS = 3  # non-zero buckets each half needs before it can be judged
SLOW_MIN_BUCKETS = 3  # non-zero buckets needed to estimate an interval from their spacing
SLOW_FRACTIONAL_MIN = 8  # non-zero buckets before a spacing between step and 2 steps counts as slow
SLOW_MARGIN = 1.25  # mean spacing this far above the step: slower than the step, not just holes
SLOW_MISS_RATIO = 1.5  # a slower-than-step series misses its cadence after this many intervals

STATE_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),
        ("series_id", pa.string()),
        ("observed", pa.float64()),
        ("expected", pa.float64()),
        ("state", pa.uint8()),
        ("flags", pa.uint16()),
    ]
)

Mode = Literal["samples", "presence"]
FailedSpan = tuple[int, int, str]  # inclusive bucket-ts range [a, b], reason


def grid(start_ms: int, end_ms: int, step_ms: int) -> list[int]:
    """Bucket end timestamps: multiples of step within [start, end], inclusive."""
    first = -(-start_ms // step_ms) * step_ms
    return list(range(first, end_ms + 1, step_ms))


def short(observed: pl.Expr, expected: pl.Expr) -> pl.Expr:
    """Below expected by more than jitter: one sample, or (1 - PARTIAL_RATIO) of a long bucket."""
    return observed < expected - pl.max_horizontal(pl.lit(1.0), (1 - PARTIAL_RATIO) * expected)


def compute(
    buckets: pa.Table,
    series_ids: Sequence[str],
    *,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    resolution_ms: int,
    mode: Mode,
    failed: Sequence[FailedSpan] = (),
    source_filled: bool = False,
    post_gap_buckets: int = 0,
) -> pa.Table:
    """`source_filled`: the counts are not observed samples (expression cannot tell), so every
    bucket is UNKNOWN + SOURCE_FILLED. `post_gap_buckets`: the source's increase/rate after a gap
    reaches back over the gap, so the first n OK/PARTIAL buckets after an EMPTY/UNKNOWN one
    (stopping at the next gap) are flagged POST_GAP (state unchanged); 0 turns it off."""
    ts = grid(start_ms, end_ms, step_ms)
    if not ts or not series_ids:
        return STATE_SCHEMA.empty_table()
    base = pl.DataFrame({"series_id": list(series_ids)}, schema={"series_id": pl.String}).join(
        pl.DataFrame({"ts_ms": ts}, schema={"ts_ms": pl.Int64}), how="cross"
    )
    rows = pl.from_arrow(buckets)
    seen = (
        pl.col("count").cast(pl.Float64)
        if mode == "samples"
        else pl.col("avg").fill_nan(None).is_not_null().cast(pl.Float64)
    )
    obs = rows.select("ts_ms", "series_id", seen.alias("observed"))
    df = base.join(obs, on=["series_id", "ts_ms"], how="left").with_columns(
        pl.col("observed").fill_null(0.0)
    )
    df = df.sort("series_id", "ts_ms")
    if mode == "samples":
        df = _with_expected(df, step_ms)
    else:
        df = df.with_columns(
            pl.lit(1.0).alias("expected"),
            pl.lit(False).alias("_slow"),
            pl.lit(None, dtype=pl.Float64).alias("_i"),
        )
    first = (
        df.filter(pl.col("observed") > 0)
        .group_by("series_id")
        .agg(pl.col("ts_ms").min().alias("first_seen"))
    )
    df = df.join(first, on="series_id", how="left")
    unknown = pl.lit(source_filled)
    for a, b, _reason in failed:
        unknown = unknown | pl.col("ts_ms").is_between(a, b)
    # a slower-than-step series is empty only once the time since its last sample exceeds its
    # cadence; faster series (and any series with no interval estimate) miss with every 0 bucket.
    # An UNKNOWN bucket resets the cadence reference: what happened inside it is not known.
    ref = pl.when((pl.col("observed") > 0) | unknown).then(pl.col("ts_ms")).forward_fill()
    interval = pl.col("_i").fill_null(0.0)
    miss_after = pl.max_horizontal(SLOW_MISS_RATIO * interval, interval + step_ms)
    df = df.with_columns(
        (~pl.col("_slow") | (pl.col("ts_ms") - ref.over("series_id") > miss_after)).alias("_missed")
    )
    state = (
        pl.when(unknown)
        .then(int(State.UNKNOWN))
        .when(pl.col("first_seen").is_not_null() & (pl.col("ts_ms") < pl.col("first_seen")))
        .then(int(State.ABSENT))
        .when(pl.col("observed") == 0)
        .then(pl.when(pl.col("_missed")).then(int(State.EMPTY)).otherwise(int(State.OK)))
        .when(pl.col("_slow"))
        .then(int(State.OK))  # a sample on a slower-than-step series holds its cadence
        .when(short(pl.col("observed"), pl.col("expected")))
        .then(int(State.PARTIAL))
        .otherwise(int(State.OK))
    )
    df = df.with_columns(
        state.cast(pl.UInt8).alias("state"),
        pl.when(unknown).then(0.0).otherwise(pl.col("observed")).alias("observed"),
    ).sort("series_id", "ts_ms")
    flags = pl.lit(int(Flag.SOURCE_FILLED) if source_filled else 0)
    if mode == "samples" and not source_filled and step_ms >= resolution_ms:
        # (a step finer than the scrape interval only ever counts 0 or 1: no rate to compare)
        df, changed = _interval_change(df)
        flags = flags | changed
    if post_gap_buckets > 0:
        is_gap = pl.col("state").is_in([int(State.EMPTY), int(State.UNKNOWN)])
        df = df.with_columns(is_gap.cum_sum().over("series_id").alias("_gap_no"))
        # position after the gap bucket that opened this run (the gap bucket itself is 0)
        pos = pl.int_range(pl.len()).over(["series_id", "_gap_no"])
        after = (
            ~is_gap
            & (pl.col("_gap_no") > 0)
            & (pl.col("state") != int(State.ABSENT))
            & (pos <= post_gap_buckets)
        )
        flags = flags | pl.when(after).then(int(Flag.POST_GAP)).otherwise(0)
    out = df.with_columns(flags.cast(pl.UInt16).alias("flags")).select(STATE_SCHEMA.names)
    return out.to_arrow().cast(STATE_SCHEMA)


def _with_expected(df: pl.DataFrame, step_ms: int) -> pl.DataFrame:
    """Adds `expected` (samples per bucket), `_slow` and `_i` (the series' sample interval in ms).
    df is sorted.

    The series' own interval, not the source's configured one (a source may scrape slower than its
    preset says). Normally the typical count of a bucket that has samples: interval = step / that.
    When that count is 1 and the non-zero buckets are spaced wider than the step, the series is
    scraped slower than the step: interval = the trimmed mean spacing (gaps up to twice the median;
    a median would snap to whole steps), expected = step / interval < 1. The spacing must exceed
    the step: median gap > step, or (with SLOW_FRACTIONAL_MIN buckets) mean > SLOW_MARGIN x step, so
    a step-rate series with a few holes is not mistaken for slow.
    `expected` carries the estimate (interval = step / expected): caveats reads it from there."""
    gaps = pl.col("ts_ms").diff().drop_nulls()
    per = (
        df.filter(pl.col("observed") > 0)
        .group_by("series_id")
        .agg(
            pl.col("observed").median().alias("_typical"),
            gaps.median().alias("_med"),
            gaps.filter(gaps <= 2 * gaps.median()).mean().alias("_gap"),
            pl.len().alias("_n"),
        )
    )
    slow = (
        (pl.col("_typical") <= 1.0)
        & (pl.col("_n") >= SLOW_MIN_BUCKETS)
        & (
            (pl.col("_med") > step_ms)
            | ((pl.col("_n") >= SLOW_FRACTIONAL_MIN) & (pl.col("_gap") > SLOW_MARGIN * step_ms))
        )
    ).fill_null(False)
    per = per.with_columns(
        slow.alias("_slow"),
        pl.when(slow)
        .then(pl.col("_gap"))
        .otherwise(step_ms / pl.max_horizontal(pl.lit(1.0), pl.col("_typical")))
        .alias("_i"),
    ).select("series_id", "_slow", "_i")
    df = df.join(per, on="series_id", how="left").with_columns(
        pl.col("_slow").fill_null(False), pl.col("_i").fill_null(float(step_ms))
    )
    return df.with_columns((step_ms / pl.col("_i")).alias("expected"))


def _interval_change(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.Expr]:
    """Series whose sample rate differs >= CHANGE_RATIO between the first and second half of their
    non-zero buckets: the half further from the series' baseline (`expected`) gets INTERVAL_CHANGE.
    Only a flag: the baseline and the states are unchanged. Returns df (sorted by series, ts)
    with helper bounds and the flag expression."""
    # (slower-than-step series show a 0/1 pattern, not a rate: not judged)
    nz = df.filter((pl.col("observed") > 0) & ~pl.col("_slow")).with_columns(
        (pl.int_range(pl.len()).over("series_id") >= pl.len().over("series_id") // 2).alias("_late")
    )
    half = {
        h: nz.filter(pl.col("_late") == (h == "late"))
        .group_by("series_id")
        .agg(
            pl.col("observed").median().alias(f"_m_{h}"),
            pl.col("observed").mean().alias(f"_mean_{h}"),
            pl.col("observed").quantile(0.25).alias(f"_q1_{h}"),
            pl.col("observed").quantile(0.75).alias(f"_q3_{h}"),
            pl.len().alias(f"_n_{h}"),
            pl.col("ts_ms").min().alias(f"_a_{h}"),
            pl.col("ts_ms").max().alias(f"_b_{h}"),
        )
        for h in ("early", "late")
    }
    exp = nz.group_by("series_id").agg(pl.col("expected").first().alias("_exp"))
    st = half["early"].join(half["late"], on="series_id").join(exp, on="series_id")
    lo = pl.min_horizontal("_mean_early", "_mean_late")
    hi = pl.max_horizontal("_mean_early", "_mean_late")
    # clear separation, not just a ratio: alternating low counts (2,1,2,1) are steady jitter
    separated = (
        pl.when(pl.col("_mean_late") >= pl.col("_mean_early"))
        .then(pl.col("_q1_late") > pl.col("_q3_early"))
        .otherwise(pl.col("_q1_early") > pl.col("_q3_late"))
    )
    dist = lambda m: (m / pl.col("_exp")).log().abs()
    late_is_odd = dist(pl.col("_m_late")) >= dist(pl.col("_m_early"))
    st = (
        st.filter(
            (pl.col("_n_early") >= CHANGE_MIN_BUCKETS)
            & (pl.col("_n_late") >= CHANGE_MIN_BUCKETS)
            & (hi >= CHANGE_RATIO * lo)
            & separated
        )
        .with_columns(
            pl.when(late_is_odd).then(pl.col("_a_late")).otherwise(pl.col("_a_early")).alias("_lo"),
            pl.when(late_is_odd).then(pl.col("_b_late")).otherwise(pl.col("_b_early")).alias("_hi"),
        )
        .select("series_id", "_lo", "_hi")
    )
    df = df.join(st, on="series_id", how="left")
    changed = pl.when(pl.col("ts_ms").is_between(pl.col("_lo"), pl.col("_hi"))).then(
        int(Flag.INTERVAL_CHANGE)
    )
    return df, changed.otherwise(0)


GROUP_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),
        ("group", pa.string()),
        ("alive", pa.int32()),
        ("reporting", pa.int32()),
        ("observed", pa.float64()),
        ("expected", pa.float64()),
        ("state", pa.uint8()),
        ("flags", pa.uint16()),
        ("silent", pa.list_(pa.string())),
    ]
)

_ALIVE = pl.col("state") != int(State.ABSENT)


def _classify(
    any_alive: pl.Expr, any_unknown: pl.Expr, nothing_seen: pl.Expr, any_gap: pl.Expr
) -> pl.Expr:
    """Shared by coarsen and merge; works from states, never re-applies jitter to sums."""
    return (
        pl.when(~any_alive)
        .then(int(State.ABSENT))
        .when(any_unknown)
        .then(int(State.UNKNOWN))
        .when(nothing_seen)
        .then(int(State.EMPTY))
        .when(any_gap)
        .then(int(State.PARTIAL))
        .otherwise(int(State.OK))
        .cast(pl.UInt8)
    )


def coarsen(states: pa.Table, new_step_ms: int) -> pa.Table:
    """Merge buckets into coarser ones ending at multiples of new_step_ms (as resample.rebucket)."""
    if states.num_rows == 0:
        return states
    k = new_step_ms
    out = (
        pl.from_arrow(states)
        .with_columns(((pl.col("ts_ms") + k - 1) // k * k).alias("ts_ms"))
        .group_by(["series_id", "ts_ms"])
        .agg(
            pl.col("observed").filter(_ALIVE).sum().alias("observed"),
            pl.col("expected").filter(_ALIVE).sum().alias("expected"),
            _ALIVE.any().alias("_alive"),
            (pl.col("state") == int(State.UNKNOWN)).any().alias("_unknown"),
            pl.col("state").is_in([int(State.PARTIAL), int(State.EMPTY)]).any().alias("_gap"),
            # state-based, not observed == 0: a slower-than-step series' fine buckets are OK with
            # no samples inside their cadence
            pl.col("state").is_in([int(State.OK), int(State.PARTIAL)]).any().alias("_seen"),
            pl.col("flags").bitwise_or().alias("flags"),
        )
        .with_columns(
            _classify(pl.col("_alive"), pl.col("_unknown"), ~pl.col("_seen"), pl.col("_gap")).alias(
                "state"
            )
        )
        .sort("series_id", "ts_ms")
        .select(STATE_SCHEMA.names)
    )
    return out.to_arrow().cast(STATE_SCHEMA)


def merge(states: pa.Table, group_of: Mapping[str, str]) -> pa.Table:
    """Combine member series per bucket (spec §5.2). Absent members are not in the denominator."""
    if states.num_rows == 0:
        return GROUP_SCHEMA.empty_table()
    reporting = pl.col("state").is_in([int(State.OK), int(State.PARTIAL)])
    out = (
        pl.from_arrow(states)
        .with_columns(
            pl.col("series_id").replace_strict(dict(group_of), default=None).alias("group")
        )
        .drop_nulls("group")
        .group_by(["group", "ts_ms"])
        .agg(
            _ALIVE.sum().cast(pl.Int32).alias("alive"),
            reporting.sum().cast(pl.Int32).alias("reporting"),
            pl.col("observed").filter(_ALIVE).sum().alias("observed"),
            pl.col("expected").filter(_ALIVE).sum().alias("expected"),
            (pl.col("state") == int(State.UNKNOWN)).any().alias("_unknown"),
            pl.col("flags").bitwise_or().alias("flags"),
            pl.col("series_id").filter(pl.col("state") == int(State.EMPTY)).sort().alias("silent"),
        )
        .with_columns(
            _classify(
                pl.col("alive") > 0,
                pl.col("_unknown"),
                pl.col("reporting") == 0,
                pl.col("reporting") < pl.col("alive"),
            ).alias("state")
        )
        .sort("group", "ts_ms")
        .select(GROUP_SCHEMA.names)
    )
    return out.to_arrow().cast(GROUP_SCHEMA)
