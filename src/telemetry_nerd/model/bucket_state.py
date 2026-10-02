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
    if mode == "samples":
        # the series' own resolution, not the source's configured one (a source may scrape
        # slower than its preset says): the typical count of a bucket that has samples
        typical = pl.col("observed").filter(pl.col("observed") > 0).median().over("series_id")
        expected = pl.max_horizontal(pl.lit(1.0), typical.fill_null(1.0))
    else:
        expected = pl.lit(1.0)
    df = df.with_columns(expected.alias("expected"))
    first = (
        df.filter(pl.col("observed") > 0)
        .group_by("series_id")
        .agg(pl.col("ts_ms").min().alias("first_seen"))
    )
    df = df.join(first, on="series_id", how="left")
    unknown = pl.lit(source_filled)
    for a, b, _reason in failed:
        unknown = unknown | pl.col("ts_ms").is_between(a, b)
    state = (
        pl.when(unknown)
        .then(int(State.UNKNOWN))
        .when(pl.col("first_seen").is_not_null() & (pl.col("ts_ms") < pl.col("first_seen")))
        .then(int(State.ABSENT))
        .when(pl.col("observed") == 0)
        .then(int(State.EMPTY))
        .when(short(pl.col("observed"), pl.col("expected")))
        .then(int(State.PARTIAL))
        .otherwise(int(State.OK))
    )
    df = df.with_columns(
        state.cast(pl.UInt8).alias("state"),
        pl.when(unknown).then(0.0).otherwise(pl.col("observed")).alias("observed"),
    ).sort("series_id", "ts_ms")
    flags = pl.lit(int(Flag.SOURCE_FILLED) if source_filled else 0)
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
            pl.col("flags").bitwise_or().alias("flags"),
        )
        .with_columns(
            _classify(
                pl.col("_alive"), pl.col("_unknown"), pl.col("observed") == 0, pl.col("_gap")
            ).alias("state")
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
