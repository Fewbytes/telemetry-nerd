import pyarrow as pa
import pytest

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, Flag, State, compute, grid
from telemetry_nerd.model.series import BUCKET_SCHEMA

STEP, RES = 60_000, 15_000  # expected 4 samples per bucket


def buckets(rows):
    """rows: (ts_ms, series_id, avg, count)"""
    return pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "avg": [r[2] for r in rows],
            "min": [r[2] for r in rows],
            "max": [r[2] for r in rows],
            "count": [r[3] for r in rows],
        },
        schema=BUCKET_SCHEMA,
    )


def states(table, sid="a"):
    return [
        s for s, i in zip(table["state"].to_pylist(), table["series_id"].to_pylist()) if i == sid
    ]


def run(rows, sids=("a",), mode="samples", failed=(), start=STEP, end=5 * STEP):
    return compute(
        buckets(rows),
        sids,
        start_ms=start,
        end_ms=end,
        step_ms=STEP,
        resolution_ms=RES,
        mode=mode,
        failed=failed,
    )


def test_grid_is_inclusive_and_step_aligned():
    assert grid(STEP + 1, 3 * STEP, STEP) == [2 * STEP, 3 * STEP]


def test_full_buckets_are_ok_and_one_sample_of_jitter_is_tolerated():
    out = run([(t * STEP, "a", 1.0, 4 if t != 3 else 3) for t in range(1, 6)])
    assert out.schema == STATE_SCHEMA
    assert states(out) == [State.OK] * 5


def test_two_of_four_samples_is_partial():
    out = run([(t * STEP, "a", 1.0, 2 if t == 2 else 4) for t in range(1, 6)])
    assert states(out)[1] == State.PARTIAL


def test_hole_is_empty_and_leading_absence_is_absent():
    rows = [(t * STEP, "a", 1.0, 4) for t in (3, 5)]  # born at 3, hole at 4
    assert states(run(rows)) == [State.ABSENT, State.ABSENT, State.OK, State.EMPTY, State.OK]


def test_trailing_silence_is_empty_not_absent():
    rows = [(t * STEP, "a", 1.0, 4) for t in (1, 2)]
    assert states(run(rows))[2:] == [State.EMPTY] * 3


def test_failed_span_is_unknown_with_zero_observed():
    rows = [(t * STEP, "a", 1.0, 4) for t in range(1, 6)]
    out = run(rows, failed=[(2 * STEP, 3 * STEP, "timeout")])
    assert states(out) == [State.OK, State.UNKNOWN, State.UNKNOWN, State.OK, State.OK]
    assert out["observed"].to_pylist()[1] == 0.0


def test_presence_mode_uses_non_null_values():
    rows = [(STEP, "a", 1.0, 0), (2 * STEP, "a", None, 7), (3 * STEP, "a", 2.0, 0)]
    assert states(run(rows, mode="presence", end=3 * STEP)) == [State.OK, State.EMPTY, State.OK]


def test_series_never_seen_is_empty_everywhere():
    out = run([(STEP, "a", 1.0, 4)], sids=("a", "b"), end=2 * STEP)
    assert states(out, "b") == [State.EMPTY, State.EMPTY]


@pytest.mark.parametrize("sids", [(), ("a",)])
def test_empty_inputs(sids):
    out = compute(
        buckets([]),
        sids,
        start_ms=STEP,
        end_ms=STEP - 1 if sids else STEP,
        step_ms=STEP,
        resolution_ms=RES,
        mode="samples",
    )
    assert out.num_rows == 0 and out.schema == STATE_SCHEMA


def test_expected_follows_the_series_not_the_configured_resolution():
    # source says 15s (expected 4/bucket) but the series is scraped every 60s: 1 sample/bucket
    out = run([(t * STEP, "a", 1.0, 1) for t in range(1, 6)])
    assert states(out) == [State.OK] * 5
    assert set(out["expected"].to_pylist()) == {1.0}


def test_coarse_scrape_hole_is_still_empty():
    out = run([(t * STEP, "a", 1.0, 1) for t in (1, 2, 4, 5)])
    assert states(out) == [State.OK, State.OK, State.EMPTY, State.OK, State.OK]


def test_recorded_wikimedia_series_has_no_partial_buckets():
    import json
    from pathlib import Path

    path = next(
        (Path(__file__).resolve().parent.parent / "fixtures" / "wikimedia").glob(
            "*query-range-1e6d4893e7*.json"
        )
    )
    raw = json.loads(json.loads(path.read_text())["response"]["body"])
    values = raw["data"]["result"][0]["values"]
    rows = [(int(t) * 1000, "a", float(v), int(float(v))) for t, v in values]
    out = compute(
        buckets(rows),
        ("a",),
        start_ms=rows[0][0],
        end_ms=rows[-1][0],
        step_ms=STEP,
        resolution_ms=15_000,  # the Wikimedia preset's, though the series is scraped every 60s
        mode="samples",
    )
    assert State.PARTIAL not in states(out) and State.EMPTY not in states(out)


def _change_rows(counts, sid="a"):
    return [(t * STEP, sid, 1.0, c) for t, c in enumerate(counts, start=1)]


def _flags(table, sid="a"):
    return [
        f for f, i in zip(table["flags"].to_pylist(), table["series_id"].to_pylist()) if i == sid
    ]


def test_rate_change_flags_the_half_that_is_not_the_baseline():
    # median of 8 non-zero counts = 2.5; the 1/bucket half is further from it than the 4/bucket half
    out = run(_change_rows([4, 4, 4, 4, 1, 1, 1, 1]), end=8 * STEP)
    assert [bool(f & int(Flag.INTERVAL_CHANGE)) for f in _flags(out)] == [False] * 4 + [True] * 4
    # the flag never changes a state: same states as without it
    assert states(out) == [State.OK] * 4 + [State.PARTIAL] * 4


def test_rate_change_flags_first_half_when_it_is_the_outlier():
    out = run(_change_rows([1, 1, 1, 4, 4, 4, 4, 4, 4]), end=9 * STEP)
    flagged = [bool(f & int(Flag.INTERVAL_CHANGE)) for f in _flags(out)]
    assert flagged == [True] * 4 + [False] * 5


def test_jittery_steady_series_has_no_rate_change():
    out = run(_change_rows([3, 4, 4, 3, 4, 4, 3, 4]), end=8 * STEP)
    assert not any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out))


def test_rate_change_needs_three_buckets_per_half():
    out = run(_change_rows([4, 4, 4, 4, 1, 1]), end=6 * STEP)  # 3 + 3 -> flagged
    assert any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out))
    out = run(_change_rows([4, 4, 4, 4, 4, 1]), end=6 * STEP)  # halves 3 + 3, medians 4 vs 4
    assert not any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out))
    out = run(_change_rows([4, 4, 1, 1, 1]), end=5 * STEP)  # 2 + 3
    assert not any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out))


def test_rate_change_not_judged_in_presence_mode_or_source_filled():
    rows = _change_rows([4, 4, 4, 4, 1, 1, 1, 1])
    out = run(rows, end=8 * STEP, mode="presence")
    assert not any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out))
    out = compute(
        buckets(rows), ("a",), start_ms=STEP, end_ms=8 * STEP, step_ms=STEP,
        resolution_ms=RES, mode="samples", source_filled=True,
    )  # fmt: skip
    assert not any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out))
