import pyarrow as pa

from telemetry_nerd.core.coverage_check import claim_coverage
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State

STEP = 60_000


def table(states, obs=None):
    n = len(states)
    obs = obs or [4.0 if s == State.OK else 0.0 for s in states]
    return pa.table({"ts_ms": [(i + 1) * STEP for i in range(n)], "series_id": ["a"] * n,
                     "observed": obs, "expected": [4.0] * n, "state": [int(s) for s in states],
                     "flags": [0] * n}, schema=STATE_SCHEMA)  # fmt: skip


def test_clean_window_passes():
    assert claim_coverage(table([State.OK] * 4), 0, 4 * STEP) == []


def test_unknown_in_window_blocks():
    [c] = claim_coverage(table([State.OK, State.UNKNOWN, State.OK]), 0, 3 * STEP)
    assert (c.code, c.severity) == ("untrusted_data", "blocks_claim")


def test_unknown_outside_window_is_ignored():
    assert claim_coverage(table([State.OK, State.OK, State.UNKNOWN]), 0, 2 * STEP) == []


def test_mostly_missing_blocks_and_little_missing_warns():
    [c] = claim_coverage(table([State.EMPTY, State.EMPTY, State.OK]), 0, 3 * STEP)
    assert c.severity == "blocks_claim"
    [c] = claim_coverage(table([State.EMPTY, State.OK, State.OK, State.OK]), 0, 4 * STEP)
    assert (c.code, c.severity) == ("missing_data", "warn")


def test_window_outside_the_data_blocks():
    [c] = claim_coverage(table([State.OK] * 3), 10 * STEP, 12 * STEP)
    assert (c.code, c.severity) == ("missing_data", "blocks_claim")


def test_window_over_only_absent_buckets_blocks():
    [c] = claim_coverage(table([State.ABSENT, State.ABSENT, State.OK]), 0, 2 * STEP)
    assert (c.code, c.severity) == ("missing_data", "blocks_claim")
