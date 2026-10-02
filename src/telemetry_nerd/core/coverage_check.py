"""Does a claim window have the data to support a claim? (spec 2026-10-02 §4.4)"""

from __future__ import annotations

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.bucket_state import State
from telemetry_nerd.model.caveats import Caveat, Where

BLOCK_BELOW = 0.5


def claim_coverage(states: pa.Table, start_ms: int, end_ms: int, step_ms: int) -> list[Caveat]:
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
    if (alive["state"] == int(State.UNKNOWN)).any():
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
