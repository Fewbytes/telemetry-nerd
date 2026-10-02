"""Does a claim window have the data to support a claim? (spec 2026-10-02 §4.4)"""

from __future__ import annotations

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.bucket_state import Flag, State
from telemetry_nerd.model.caveats import UNOBSERVABLE_MESSAGE, Caveat, Where, _total, runs

BLOCK_BELOW = 0.5


def claim_coverage(states: pa.Table, start_ms: int, end_ms: int, step_ms: int) -> list[Caveat]:
    return [
        *_coverage_verdict(states, start_ms, end_ms, step_ms),
        *_post_gap(states, start_ms, end_ms, step_ms),
    ]


def _post_gap(states: pa.Table, start_ms: int, end_ms: int, step_ms: int) -> list[Caveat]:
    """Values right after a gap that the source computed from before the gap (not real spikes)."""
    df = pl.from_arrow(states).filter(
        (pl.col("ts_ms") > start_ms)
        & (pl.col("ts_ms") - step_ms < end_ms)
        & (pl.col("state") != int(State.UNKNOWN))
        & ((pl.col("flags") & int(Flag.POST_GAP)) != 0)
    )
    if df.is_empty():
        return []
    spans = runs(df["ts_ms"].unique().to_list(), step_ms)
    return [Caveat(code="post_gap_spike", severity="warn", where=Where(spans=spans),
                   source="validator",
                   message=f"Values right after a gap ({_total(spans)} in the claim window) are "
                   "computed from the sample before the gap, not real spikes; do not cite them "
                   "as one.")]  # fmt: skip


def _coverage_verdict(states: pa.Table, start_ms: int, end_ms: int, step_ms: int) -> list[Caveat]:
    """A bucket ending at t covers (t - step, t]; it counts when that overlaps the claim window."""
    df = pl.from_arrow(states).filter(
        (pl.col("ts_ms") > start_ms) & (pl.col("ts_ms") - step_ms < end_ms)
    )
    alive = df.filter(pl.col("state") != int(State.ABSENT))
    where = Where(spans=[(start_ms, end_ms)])
    if alive.is_empty():
        return [Caveat(code="missing_data", severity="blocks_claim", where=where,
                       source="validator",
                       message="No data from this dataset in the claim window.")]  # fmt: skip
    unknown = alive.filter(pl.col("state") == int(State.UNKNOWN))
    if ((unknown["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():
        return [Caveat(code="untrusted_data", severity="blocks_claim", where=where,
                       source="validator", message=UNOBSERVABLE_MESSAGE + " Re-query a simpler "
                       "expression (e.g. split it into its selectors) instead of retrying "
                       "this one.")]  # fmt: skip
    if unknown.height:
        return [Caveat(code="untrusted_data", severity="blocks_claim", where=where,
                       source="validator", message="The claim window contains data the source "
                       "could not return (fetch failed or unknown).")]  # fmt: skip
    exp = alive["expected"].sum()
    share = min(1.0, alive["observed"].sum() / exp) if exp else 0.0
    if share < BLOCK_BELOW:
        return [Caveat(code="missing_data", severity="blocks_claim", where=where,
                       source="validator", message=f"Only {share:.0%} of expected samples in the "
                       "claim window.")]  # fmt: skip
    if (alive["state"] != int(State.OK)).any():
        return [Caveat(code="missing_data", severity="warn", where=where, source="validator",
                       message=f"{share:.0%} of expected samples in the claim window.")]  # fmt: skip
    return []
