"""bucket_state: per series and bucket, how much of the expected data arrived and whether it can
be trusted (spec 2026-10-02 §5). Computed on read from bucket counts; never stored."""

from __future__ import annotations

from collections.abc import Sequence
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
    SOURCE_FILLED = 8


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
) -> pa.Table:
    ts = grid(start_ms, end_ms, step_ms)
    if not ts or not series_ids:
        return STATE_SCHEMA.empty_table()
    expected = max(1.0, step_ms / resolution_ms) if mode == "samples" else 1.0
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
        pl.col("observed").fill_null(0.0), pl.lit(expected).alias("expected")
    )
    first = (
        df.filter(pl.col("observed") > 0)
        .group_by("series_id")
        .agg(pl.col("ts_ms").min().alias("first_seen"))
    )
    df = df.join(first, on="series_id", how="left")
    unknown = pl.lit(False)
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
    out = (
        df.with_columns(
            state.cast(pl.UInt8).alias("state"),
            pl.lit(0, pl.UInt16).alias("flags"),
            pl.when(unknown).then(0.0).otherwise(pl.col("observed")).alias("observed"),
        )
        .sort("series_id", "ts_ms")
        .select(STATE_SCHEMA.names)
    )
    return out.to_arrow().cast(STATE_SCHEMA)
