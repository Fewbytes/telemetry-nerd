import re

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


def test_samples_without_a_value_are_not_empty():
    """A bucket whose samples arrived but whose expression gave no value (null avg, count > 0)
    reads by its samples, not as an empty hole (1h9.16)."""
    out = run([(t * STEP, "a", None if t == 3 else 1.0, 4) for t in range(1, 6)])
    assert states(out) == [State.OK] * 5


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
    out = run(_change_rows([4, 4, 4, 1, 1, 1]), end=6 * STEP)  # 3 + 3 -> flagged
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


def test_alternating_low_counts_are_steady_not_a_rate_change():
    for counts in ([2, 1, 2, 1, 2, 1], [2, 1] * 5):
        out = run(_change_rows(counts), end=len(counts) * STEP)
        assert not any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out)), counts


def test_clear_and_noisy_rate_changes_are_flagged():
    for counts in ([4, 4, 4, 1, 1, 1], [4, 3, 4, 4, 1, 1, 2, 1]):
        out = run(_change_rows(counts), end=len(counts) * STEP)
        assert any(f & int(Flag.INTERVAL_CHANGE) for f in _flags(out)), counts


# --- series scraped slower than the step (fm1) ---

S15 = 15_000  # query step finer than the series' real scrape interval


def _slow_run(sample_ts, *, end, step=S15, start=S15, res=S15):
    rows = [(t, "a", 1.0, 1) for t in sample_ts]
    return compute(
        buckets(rows),
        ("a",),
        start_ms=start,
        end_ms=end,
        step_ms=step,
        resolution_ms=res,
        mode="samples",
    )


def _cadence(interval_ms, first, end):
    return list(range(first, end + 1, interval_ms))


HOUR = 3_600_000


@pytest.mark.parametrize("phase", [0, 15_000, 30_000, 45_000])
def test_slow_cadence_at_a_finer_step_is_all_ok(phase):
    first = 60_000 + phase
    out = _slow_run(_cadence(60_000, first, HOUR), end=HOUR)
    st = states(out)
    assert set(st) <= {State.OK, State.ABSENT}
    # leading buckets before the first sample are ABSENT, everything after holds the cadence
    assert State.EMPTY not in st and State.PARTIAL not in st
    alive = [i for i, s in enumerate(st) if s != State.ABSENT]
    exp = [e for e, s in zip(out["expected"].to_pylist(), st) if s != State.ABSENT]
    obs = [o for o, s in zip(out["observed"].to_pylist(), st) if s != State.ABSENT]
    assert alive and set(exp) == {0.25}
    assert sum(obs) / sum(exp) == pytest.approx(1.0, abs=0.02)


def test_slow_cadence_with_a_real_hole_goes_empty_after_one_and_a_half_intervals():
    last_before, resume = 20 * 60_000, 30 * 60_000  # 10 minute hole
    ts = _cadence(60_000, 60_000, last_before) + _cadence(60_000, resume, HOUR)
    out = _slow_run(ts, end=HOUR)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    empty = sorted(t for t, s in by_ts.items() if s == State.EMPTY)
    assert empty
    assert empty[0] - last_before > 90_000 and empty[0] - last_before <= 90_000 + S15
    assert empty[-1] < resume and by_ts[last_before + 90_000] == State.OK
    assert all(by_ts[t] == State.EMPTY for t in range(empty[0], resume, S15))
    assert by_ts[resume] == State.OK
    exp = sum(out["expected"].to_pylist())
    assert sum(out["observed"].to_pylist()) / exp < 0.95


def test_irregular_slow_cadence_has_no_empty():
    ts, t = [], 30_000
    for i in range(80):
        ts.append(t)
        t += 30_000 if i % 2 == 0 else 45_000  # a 40s scrape seen through a 15s grid
    out = _slow_run(ts, end=ts[-1])
    assert State.EMPTY not in states(out) and State.PARTIAL not in states(out)
    assert out["flags"].to_pylist() == [0] * out.num_rows


def test_slow_series_trailing_silence_goes_empty_after_one_and_a_half_intervals():
    end = HOUR
    last = end - 5 * 60_000
    out = _slow_run(_cadence(60_000, 60_000, last), end=end)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert by_ts[last + 90_000] == State.OK
    assert all(by_ts[t] == State.EMPTY for t in range(last + 90_000 + S15, end + 1, S15))


def test_slow_series_never_partial_and_flags_its_interval_change():
    # 60s -> 120s at a 15s step: judged from the gaps (7kc), not the 0/1 counts
    ts = _cadence(60_000, 60_000, HOUR // 2) + _cadence(120_000, HOUR // 2 + 120_000, HOUR)
    out = _slow_run(ts, end=HOUR)
    assert State.PARTIAL not in states(out) and State.EMPTY not in states(out)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["flags"].to_pylist()))
    # one side is flagged (here the 60s one: it covers slightly less of the window), not both
    assert bool(by_ts[HOUR] & Flag.INTERVAL_CHANGE) != bool(by_ts[HOUR // 4] & Flag.INTERVAL_CHANGE)


def test_slow_cadence_feeds_summary_coverage_and_claim_share():
    from telemetry_nerd.core.coverage_check import claim_coverage

    out = _slow_run(_cadence(60_000, 60_000, HOUR), end=HOUR)
    assert claim_coverage(out, 10 * 60_000, 50 * 60_000, S15) == []


def _scrape_rows(interval_ms, step, end=HOUR, first=None):
    """Samples every interval_ms, counted into the step grid's buckets (a bucket ends at its ts)."""
    counts: dict[int, int] = {}
    t = interval_ms if first is None else first
    while t <= end:
        b = -(-t // step) * step
        counts[b] = counts.get(b, 0) + 1
        t += interval_ms
    return [(b, "a", 1.0, c) for b, c in sorted(counts.items())]


def _compute_rows(rows, *, step, end=HOUR, failed=(), res=None, known=True):
    return compute(
        buckets(rows),
        ("a",),
        start_ms=step,
        end_ms=end,
        step_ms=step,
        resolution_ms=res or step,
        mode="samples",
        failed=failed,
        interval_known=known,
    )


@pytest.mark.parametrize(
    ("interval", "step"), [(20_000, 15_000), (25_000, 15_000), (40_000, 30_000), (40_000, 15_000)]
)
def test_fractionally_slow_series_has_no_empty_and_coverage_near_one(interval, step):
    from telemetry_nerd.model.caveats import differing_intervals

    out = _compute_rows(_scrape_rows(interval, step), step=step)
    st = states(out)
    assert State.EMPTY not in st and State.PARTIAL not in st
    alive = [
        (o, e)
        for o, e, s in zip(out["observed"].to_pylist(), out["expected"].to_pylist(), st)
        if s != State.ABSENT
    ]
    cov = sum(o for o, _ in alive) / sum(e for _, e in alive)
    assert 0.9 <= cov <= 1.1
    [secs] = differing_intervals(out, step, step // 4).values()
    assert abs(secs * 1000 - interval) <= step


def test_fractionally_slow_series_with_a_real_hole_is_empty():
    rows = [r for r in _scrape_rows(20_000, 15_000) if not 1_200_000 < r[0] <= 1_800_000]
    st = states(_compute_rows(rows, step=15_000))
    assert st.count(State.EMPTY) > 20


def test_coarsen_keeps_a_healthy_slow_series_ok():
    from telemetry_nerd.model.bucket_state import coarsen

    out = _slow_run(_cadence(60_000, 60_000, HOUR), end=HOUR)
    for k in (2, 3):
        assert State.EMPTY not in coarsen(out, k * S15)["state"].to_pylist()


def test_coarsen_over_a_real_hole_is_empty_where_fine_buckets_were():
    from telemetry_nerd.model.bucket_state import coarsen

    ts = _cadence(60_000, 60_000, 20 * 60_000) + _cadence(60_000, 30 * 60_000, HOUR)
    out = _slow_run(ts, end=HOUR)
    coarse = coarsen(out, 3 * S15)
    by_ts = dict(zip(coarse["ts_ms"].to_pylist(), coarse["state"].to_pylist()))
    assert by_ts[1_485_000] == State.EMPTY and by_ts[585_000] == State.OK


def test_slow_series_does_not_go_empty_right_after_an_unknown_span():
    ts = _cadence(60_000, 60_000, HOUR)
    rows = [(t, "a", 1.0, 1) for t in ts if not 20 * 60_000 <= t <= 30 * 60_000]
    out = _compute_rows(rows, step=S15, failed=[(20 * 60_000, 30 * 60_000, "boom")], res=S15)
    st = states(out)
    assert State.EMPTY not in st and State.UNKNOWN in st


# --- local cadence (7kc): rate changes, jitter at step == interval, short windows ---


def _sample_ts(interval, *, phase=0, jitter=0, end=HOUR, seed=0, start=0):
    """Scrape timestamps every `interval` from `phase`, each off by a deterministic pseudo-random
    jitter in [-jitter, +jitter] ms; only those in (start, end] (what the source would return)."""
    import random

    rng = random.Random(seed)
    out, k = [], 0
    while True:
        nominal = start + phase + k * interval
        if nominal > end + jitter:
            return out
        t = nominal + (rng.randint(-jitter, jitter) if jitter else 0)
        if start < t <= end:
            out.append(t)
        k += 1


def _count_rows(ts, step, sid="a"):
    counts: dict[int, int] = {}
    for t in ts:
        b = -(-t // step) * step
        counts[b] = counts.get(b, 0) + 1
    return [(b, sid, 1.0, c) for b, c in sorted(counts.items())]


def _alive_coverage(out, sid="a"):
    rows = [
        (o, e)
        for o, e, s, i in zip(
            out["observed"].to_pylist(),
            out["expected"].to_pylist(),
            out["state"].to_pylist(),
            out["series_id"].to_pylist(),
        )
        if i == sid and s != State.ABSENT
    ]
    return sum(o for o, _ in rows) / sum(e for _, e in rows)


def _miss_after(interval, step):
    return max(1.5 * interval, interval + step)


SWEEP = [
    (interval, step, phase, jitter)
    for interval in (15_000, 20_000, 22_500, 25_000, 40_000, 45_000, 60_000, 90_000, 120_000)
    for step in (15_000, 30_000, 60_000)
    for phase in (0, 1_000, 7_500, 14_000)
    for jitter in (0, 2_000)
]


@pytest.mark.parametrize(("interval", "step", "phase", "jitter"), SWEEP)
def test_sweep_healthy_series_reads_ok_with_coverage_near_one(interval, step, phase, jitter):
    ts = _sample_ts(interval, phase=phase, jitter=jitter, seed=interval + step + phase)
    out = _compute_rows(_count_rows(ts, step), step=step, res=15_000)
    st = states(out)
    assert State.EMPTY not in st and State.PARTIAL not in st
    assert 0.9 <= _alive_coverage(out) <= 1.1
    assert not any(f & Flag.INTERVAL_CHANGE for f in out["flags"].to_pylist())


@pytest.mark.parametrize(("interval", "step", "phase", "jitter"), SWEEP)
def test_sweep_ten_minute_hole_is_empty_inside_and_nowhere_else(interval, step, phase, jitter):
    h0, h1 = 25 * 60_000, 35 * 60_000
    ts = _sample_ts(interval, phase=phase, jitter=jitter, seed=interval + step + phase)
    ts = [t for t in ts if not h0 < t <= h1]
    bucket = lambda t: -(-t // step) * step
    p_b = bucket(max(t for t in ts if t <= h0))  # last bucket with a sample before the hole
    n_b = bucket(min(t for t in ts if t > h1))  # first bucket with a sample after it
    out = _compute_rows(_count_rows(ts, step), step=step, res=15_000)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    empty = [b for b, s in by_ts.items() if s == State.EMPTY]
    partial = [b for b, s in by_ts.items() if s == State.PARTIAL]
    assert all(p_b < b < n_b for b in empty), (p_b, n_b, empty)
    assert all(b in (p_b, n_b) for b in partial), (p_b, n_b, partial)
    # the hole interior: past the cadence allowance (local estimate within 10%) until samples resume
    interior = [b for b in by_ts if p_b + _miss_after(1.1 * interval, step) < b < n_b]
    assert interior and all(by_ts[b] == State.EMPTY for b in interior)


@pytest.mark.parametrize(("fast", "slow"), [(15_000, 60_000), (60_000, 15_000)])
# (jittered at a 30s step the change is not always flagged: not covered)
@pytest.mark.parametrize(("step", "jitter"), [(S15, 0), (S15, 2_000), (30_000, 0)])
def test_rate_change_within_the_window_reads_ok_and_flags_interval_change(fast, slow, step, jitter):
    half = HOUR // 2
    ts = _sample_ts(fast, end=half, jitter=jitter, seed=1) + _sample_ts(
        slow, start=half, end=HOUR, jitter=jitter, seed=2
    )
    out = _compute_rows(_count_rows(ts, step), step=step, res=S15)
    st = states(out)
    assert State.EMPTY not in st and State.PARTIAL not in st
    assert any(f & Flag.INTERVAL_CHANGE for f in out["flags"].to_pylist())
    assert 0.9 <= _alive_coverage(out) <= 1.1


@pytest.mark.parametrize(("first", "then"), [(15_000, 60_000), (60_000, 15_000)])
def test_rate_change_caveat_reports_both_intervals_in_order(first, then):
    from telemetry_nerd.model.caveats import from_bucket_state

    half = HOUR // 2
    ts = _sample_ts(first, end=half) + _sample_ts(then, start=half, end=HOUR)
    out = _compute_rows(_count_rows(ts, S15), step=S15, res=S15)
    [c] = [c for c in from_bucket_state(out, {"a": "A"}, S15) if c.code == "interval_change"]
    # "about": the smoothed boundary may put a sample on the wrong side
    a, b = (int(x) for x in re.search(r"every (\d+)s → (\d+)s", c.message).groups())
    assert abs(a * 1000 - first) <= 0.05 * first and abs(b * 1000 - then) <= 0.05 * then


@pytest.mark.parametrize("seed", range(5))
def test_step_equal_to_interval_with_jitter_has_no_empty(seed):
    # scrapes straddle the bucket boundaries: counts 0/1/2 with every sample present
    ts = _sample_ts(15_000, jitter=2_000, seed=seed)
    out = _compute_rows(_count_rows(ts, S15), step=S15, res=S15)
    assert State.EMPTY not in states(out)
    assert 0.95 <= _alive_coverage(out) <= 1.05


def test_step_equal_to_interval_with_jitter_still_shows_lost_scrapes():
    ts = _sample_ts(15_000, jitter=2_000, seed=3)
    lost = set(ts[20::23][:10])  # ten isolated lost scrapes
    out = _compute_rows(_count_rows([t for t in ts if t not in lost], S15), step=S15, res=S15)
    assert states(out).count(State.EMPTY) >= 5


def test_clustered_losses_in_a_fractionally_slow_series_are_not_hidden_ok():
    # a 1.2x-step series: clustered losses (adjacent lost scrapes) used to drag the local
    # interval estimate past SLOW_MARGIN, reading as cadence skips (OK) instead of EMPTY
    step, interval, end = 30_000, 36_093, 3_600_000
    lost = {3_284_463, 3_320_556, 3_356_649, 3_428_835, 3_464_928}
    ts = _sample_ts(interval, seed=1013, end=end)
    out = _compute_rows(
        _count_rows([t for t in ts if t not in lost], step), step=step, end=end, res=step
    )
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    obs_by_ts = dict(zip(out["ts_ms"].to_pylist(), out["observed"].to_pylist()))
    for t in lost:
        b = -(-t // step) * step
        assert not (obs_by_ts[b] == 0 and by_ts[b] == State.OK), (b, State(by_ts[b]).name)


@pytest.mark.parametrize(("interval", "end"), [(120_000, 10 * 60_000), (60_000, 6 * 60_000)])
def test_few_non_zero_buckets_within_cadence_are_not_empty(interval, end):
    out = _compute_rows(_count_rows(_sample_ts(interval, end=end), S15), step=S15, end=end)
    assert State.EMPTY not in states(out) and State.PARTIAL not in states(out)


@pytest.mark.parametrize("ts", [[], [90_000], [90_000, 210_000]])
def test_one_or_two_samples_do_not_crash(ts):
    out = _compute_rows(_count_rows(ts, S15), step=S15, end=10 * 60_000)
    assert out.num_rows == 40


def test_many_series_stay_fast():
    import time

    step, end = 15_000, 1440 * 15_000
    rows = []
    for i in range(20):
        rows += _count_rows(_sample_ts(15_000, jitter=2_000, seed=i, end=end), step, sid=f"s{i}")
    t0 = time.perf_counter()
    out = compute(
        buckets(rows), [f"s{i}" for i in range(20)], start_ms=step, end_ms=end, step_ms=step,
        resolution_ms=step, mode="samples",
    )  # fmt: skip
    assert out.num_rows == 20 * 1440
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.parametrize(("interval", "step"), [(45_000, 30_000), (90_000, 60_000), (22_500, S15)])
@pytest.mark.parametrize("jitter", [0, 2_000, 5_000])
@pytest.mark.parametrize("seed", range(3))
def test_steady_cadence_near_one_and_a_half_steps_is_not_a_rate_change(
    interval, step, jitter, seed
):
    # gaps alternate one and two steps: a steady rate, not a change between them
    ts = _sample_ts(interval, phase=1_300 * seed, jitter=jitter, seed=seed)
    out = _compute_rows(_count_rows(ts, step), step=step, res=15_000)
    assert not any(f & Flag.INTERVAL_CHANGE for f in out["flags"].to_pylist())
    assert State.EMPTY not in states(out) and State.PARTIAL not in states(out)


def _keep_every(ts, k, lo, hi):
    """Keep every k-th sample within [lo, hi): sustained partial loss."""
    return [t for i, t in enumerate(ts) if not lo <= t < hi or i % k == 0]


@pytest.mark.parametrize(
    ("interval", "step", "k", "lo", "hi"),
    [
        (5_000, 30_000, 4, 1_200_000, 2_400_000),
        (5_000, 60_000, 3, 600_000, 3_000_000),
        (1_000, 60_000, 4, 600_000, 3_000_000),
        (10_000, 60_000, 2, 1_200_000, 2_400_000),
    ],
)
def test_sustained_partial_loss_shows_in_coverage(interval, step, k, lo, hi):
    full = _sample_ts(interval, jitter=interval // 10)
    kept = _keep_every(full, k, lo, hi)
    out = _compute_rows(_count_rows(kept, step), step=step, res=15_000)
    assert _alive_coverage(out) == pytest.approx(len(kept) / len(full), abs=0.05)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert all(by_ts[b] == State.PARTIAL for b in by_ts if lo + step <= b < hi)
    assert all(by_ts[b] == State.OK for b in by_ts if b < lo or b > hi + step)


def test_claim_over_a_stretch_keeping_a_quarter_of_its_samples_is_blocked():
    from telemetry_nerd.core.coverage_check import claim_coverage

    lo, hi = 600_000, 3_000_000
    out = _compute_rows(_count_rows(_keep_every(_sample_ts(1_000), 4, lo, hi), 60_000), step=60_000)
    [c] = claim_coverage(out, lo, hi, 60_000)
    assert c.severity == "blocks_claim"


def test_a_leading_spill_gives_no_credit_to_a_later_lost_scrape():
    # first scrape late (its 0 bucket lies before the series is first seen: the first bucket holds
    # 2), then early, late from scrape 180 (a 0) and early again from 200 (its 2). One scrape lost
    # at 120 must read empty, not be paired with the leading 2.
    late = lambda k: k == 1 or 180 <= k < 200
    ts = [15_000 * k + (1_000 if late(k) else -1_000) for k in range(1, 241)]
    lost = ts[119]
    out = _compute_rows(_count_rows([t for t in ts if t != lost], S15), step=S15, res=S15)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert by_ts[120 * S15] == State.EMPTY
    assert states(out).count(State.EMPTY) == 1


def test_two_lost_scrapes_not_followed_at_once_by_a_spill_are_both_empty():
    # scrapes late until 123, early from 124 (bucket 124 holds 2, its 0 before the window); two
    # lost at 119-120: counts ...1 1 0 0 1 1 2..., the 2 not next to the run: both read empty
    ts = [15_000 * k + (1_000 if k <= 123 else -1_000) for k in range(1, 241)]
    kept = [t for k, t in enumerate(ts, start=1) if k not in (119, 120)]
    out = _compute_rows(_count_rows(kept, S15), step=S15, res=S15)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["observed"].to_pylist()))
    assert [by_ts[k * S15] for k in range(116, 126)] == [1, 1, 1, 1, 0, 0, 1, 1, 2, 1]
    assert states(out).count(State.EMPTY) == 2


# --- window edges (430): a neighbourhood cut short by the window edge, a 00 run at UNKNOWN ---


def test_step_rate_series_does_not_read_slow_near_the_window_start():
    # 60s scrapes in 60s buckets, early at 2, 4 and 7, late otherwise: counts 2 0 2 0 1 2 0 1 1...
    # The two gaps before bucket 360 hold 3 samples in 240s (the first bucket's 2 opens the series,
    # its gap unseen): 80s, slower than the step, for that one neighbourhood. Read slow, the 2 at
    # 420 lost its spill pairing and the 0 at 300 (its spilled scrape) read EMPTY.
    ts = [60_000 * k + (-1_000 if k in (2, 4, 7) else 1_000) for k in range(1, 60)]
    rows = _count_rows(ts, 60_000)
    counts = {b // 60_000: c for b, _, _, c in rows}
    assert [counts.get(b, 0) for b in range(2, 11)] == [2, 0, 2, 0, 1, 2, 0, 1, 1]
    out = _compute_rows(rows, step=60_000, res=15_000)
    assert State.EMPTY not in states(out) and State.PARTIAL not in states(out)
    assert 0.95 <= _alive_coverage(out) <= 1.05


@pytest.mark.parametrize(
    ("interval", "phase", "seed"),
    [
        (15_000, 0, 33),  # a false EMPTY at the window start
        (15_000, 1_000, 104),
        (15_000, 1_000, 15),  # at the window end
        (15_000, 1_000, 18),
        (60_000, 0, 33),
        (60_000, 1_000, 104),  # at both
    ],
)
def test_step_equal_to_interval_with_phase_near_a_bucket_boundary_has_no_empty(
    interval, phase, seed
):
    ts = _sample_ts(interval, phase=phase, jitter=2_000, seed=seed)
    out = _compute_rows(_count_rows(ts, interval), step=interval, res=15_000)
    assert State.EMPTY not in states(out)


@pytest.mark.parametrize("unknown_before", [False, True])
def test_two_zero_buckets_next_to_an_unknown_span_are_not_exactly_two(unknown_before):
    # scrapes late until 107, early from 108; 105 and 106 lost: counts ... 1 0 0 2 1 ... at
    # buckets 105-109. Bounded by samples the run is exactly two 0s (a lost scrape next to one that
    # spilled into the 2): the second is paired. After an UNKNOWN span the run may be longer than
    # it shows: no pairing, both read EMPTY
    ts = [S15 * k + (1_000 if k <= 107 else -1_000) for k in range(1, 240)]
    kept = [t for k, t in enumerate(ts, start=1) if k not in (105, 106)]
    failed = [(100 * S15, 105 * S15, "boom")] if unknown_before else ()
    out = _compute_rows(_count_rows(kept, S15), step=S15, res=S15, failed=failed)
    by_ts = dict(zip(out["ts_ms"].to_pylist(), out["observed"].to_pylist()))
    assert [by_ts[k * S15] for k in range(106, 110)] == [0, 0, 2, 1]
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert st[106 * S15] == State.EMPTY
    assert st[107 * S15] == (State.EMPTY if unknown_before else State.OK)


# --- window edges (9li): loss clusters at the edge stay EMPTY, a faster stretch before a slower ---


def _lost_states(n, lost):
    """15s scrapes mid-bucket (no spills) in n 15s buckets, those of buckets `lost` (1-based)
    dropped; returns {bucket number: state}."""
    ts = [S15 * k - S15 // 2 for k in range(1, n + 1) if k not in lost]
    out = _compute_rows(_count_rows(ts, S15), step=S15, end=n * S15, res=S15)
    return {t // S15: s for t, s in zip(out["ts_ms"].to_pylist(), out["state"].to_pylist())}


@pytest.mark.parametrize("lost", [(7, 9, 11), (3, 5, 7), (3, 5, 8)])
@pytest.mark.parametrize("where", ["start", "end"])
def test_loss_cluster_at_the_window_edge_is_empty_exactly_there(lost, where):
    # three lost scrapes within ~10 gaps of the edge: a neighbourhood that short around them reads
    # slower than the step (Σgap / Σsamples over 1.25 x step) though the series is at step rate. A
    # mid-series 16-gap neighbourhood needs 5 such losses; the edge must not need fewer
    n = 240
    if where == "end":
        lost = tuple(n + 1 - k for k in lost)
    st = _lost_states(n, lost)
    assert sorted(k for k, s in st.items() if s == State.EMPTY) == sorted(lost)


@pytest.mark.parametrize("n", [20, 34])
def test_loss_cluster_in_a_short_series_is_empty_exactly_there(n):
    # every neighbourhood is cut short by both edges
    st = _lost_states(n, (5, 7, 9))
    assert [k for k, s in st.items() if s == State.EMPTY] == [5, 7, 9]
    assert {s for k, s in st.items() if k not in (5, 7, 9)} == {State.OK}


def test_two_lost_scrapes_and_a_spill_at_the_window_start_stay_empty():
    # a step-rate series near a bucket boundary (0 2 pairs) that lost the scrapes of buckets 3 and
    # 5: the stretch from the edge to its change point (8 gaps, 11 samples in 210s) reads slow, but
    # holds 2s and is not slow with one sample more, so it does not hide the two
    counts = "0101011102020201111112110111112021111021" + "1" * 200
    rows = [((i + 1) * S15, "a", 1.0, int(c)) for i, c in enumerate(counts) if c != "0"]
    st = states(_compute_rows(rows, step=S15, res=S15))
    assert [i + 1 for i, s in enumerate(st[:40]) if s == State.EMPTY] == [3, 5]


@pytest.mark.parametrize(("fast", "lost"), [(12, 4), (14, 5), (20, 9)])
def test_lost_scrape_in_a_faster_stretch_at_the_window_start_is_empty(fast, lost):
    # 15s scrapes, then 60s ones: the 16 gaps after a fast gap reach into the 60s stretch and read
    # slow, which hid the lost scrape. The fast stretch is 4x faster than the rest of the edge
    # span and at the step's rate even with one sample fewer (11+ gaps: 1 loss in a shorter one
    # looks like a 20s stretch, gaps 15 30 15 15): it has no slow gaps
    ts = [16_000 + S15 * i for i in range(fast)]
    ts += list(range(ts[-1] + 60_000, HOUR, 60_000))
    lost_t = ts.pop(lost)
    out = _compute_rows(_count_rows(ts, S15), step=S15, res=S15)
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert st[-(-lost_t // S15) * S15] == State.EMPTY
    assert list(st.values()).count(State.EMPTY) == 1


def test_faster_stretch_that_is_itself_slower_than_the_step_keeps_its_cadence():
    # 30s scrapes, then 120s ones, in 15s buckets: the 30s stretch is faster than the rest but
    # still slower than the step, so its 0 buckets hold its cadence
    ts = [16_000 + 30_000 * i for i in range(12)]
    ts += list(range(ts[-1] + 120_000, HOUR, 120_000))
    out = _compute_rows(_count_rows(ts, S15), step=S15, res=S15)
    assert State.EMPTY not in states(out)


@pytest.mark.parametrize("where", ["start", "end"])
def test_loss_in_a_faster_stretch_of_a_series_that_spills_stays_empty(where):
    # 10s scrapes at a 15s step (counts 1 2 1 2 ...: spill-like 2s) at the window edge after 45s
    # ones, seven of them lost. Taking slowness away there would change which 0s pair with a 2
    # and unpair a lost one: in a series that may spill, the faster stretch keeps 430's reading
    ph, n = 6519, 25
    slow = list(range(ph + 45_000, HOUR - n * 10_000, 45_000))
    ts = slow + [slow[-1] + 10_000 * (i + 1) for i in range(n)]
    ts = [t for i, t in enumerate(ts) if i not in {80, 89, 91, 93, 94, 95, 97}]
    if where == "start":
        ts = sorted(HOUR + S15 - t for t in ts)
    out = _compute_rows(_count_rows(ts, S15), step=S15, res=S15)
    empty = {t for t, s in zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()) if s == 2}
    lost = {3_555_000, 3_585_000}
    if where == "start":
        lost = {-(-(HOUR + S15 - t) // S15) * S15 for t in (3_555_000 - 7_500, 3_585_000 - 7_500)}
    assert lost <= empty


def test_lost_scrapes_in_faster_stretches_at_both_edges_are_empty():
    # a short series: 15s scrapes, 60s ones, 15s ones again; one scrape lost in each fast part
    ts = [S15 * k - S15 // 2 for k in range(1, 15)]
    ts += [ts[-1] + 60_000 * i for i in range(1, 31)]
    ts += [ts[-1] + S15 * i for i in range(1, 15)]
    end = -(-ts[-1] // S15) * S15
    lost = (ts[4], ts[-5])
    kept = [t for t in ts if t not in lost]
    out = _compute_rows(_count_rows(kept, S15), step=S15, end=end, res=S15)
    empty = [t for t, s in zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()) if s == 2]
    assert empty == [-(-t // S15) * S15 for t in lost]


def _slow_after(fast_i, slow_i, n, phase, lost=()):
    ts = [phase + fast_i * i for i in range(n)]
    ts += list(range(ts[-1] + slow_i, HOUR, slow_i))
    kept = [t for i, t in enumerate(ts) if i not in set(lost)]
    out = _compute_rows(_count_rows(kept, S15), step=S15, res=S15)
    empty = {t for t, s in zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()) if s == 2}
    lost_b = {-(-ts[i] // S15) * S15 for i in lost}
    return empty, lost_b


def test_short_20s_stretch_before_50s_ones_is_not_turned_empty():
    # 5 scrapes 20s apart (gaps 15 30 15 15: Σgap / Σsamples = 1.25 x step, on the margin by the
    # snap of so few gaps) before 50s ones: slower than the step, its 0 bucket holds the cadence
    empty, _ = _slow_after(20_000, 50_000, 5, 7_289)
    assert empty == set()


def test_long_20s_stretch_with_losses_before_50s_ones_reads_as_430():
    # a slower-than-step stretch is not a step-rate one: the faster-stretch rule leaves it, and
    # it reads exactly as in 430 (slow: a 0 is EMPTY only once the cadence is missed). Before the
    # one-sample-fewer check the rule read 11 more of its buckets EMPTY
    lost = (3, 8, 12, 13, 14, 15, 16, 17, 69, 82)
    empty, _ = _slow_after(20_000, 50_000, 37, 2_448, lost)
    assert sorted(t // 1000 for t in empty) == [120, 270, 285, 300, 315, 330, 345, 360, 2415]


def test_duplicate_series_ts_rows_are_refused():
    rows = [(t * STEP, "a", 1.0, 4) for t in range(1, 6)] + [(3 * STEP, "a", 1.0, 4)]
    with pytest.raises(ValueError, match="one row per"):
        run(rows)
    # the same ts on different series is not a duplicate
    run([(STEP, "a", 1.0, 4), (STEP, "b", 1.0, 4)], sids=("a", "b"), end=STEP)


@pytest.mark.parametrize(
    "spans",
    [
        [(2, 2)],
        [(4, 5), (2, 3)],  # unsorted, adjacent
        [(2, 6), (3, 4)],  # nested
        [(2, 4), (3, 7), (9, 9)],  # overlapping, then apart
        [(0, 1), (12, 40)],  # touching the window edges from outside
        [(7, 8)],  # none of the buckets
    ],
)
def test_failed_spans_mark_exactly_the_buckets_inside_any_of_them(spans):
    n = 10
    rows = [(t * STEP, "a", 1.0, 4) for t in range(1, n + 1)]
    out = run(rows, failed=[(a * STEP, b * STEP, "x") for a, b in spans], end=n * STEP)
    want = [any(a <= t <= b for a, b in spans) for t in range(1, n + 1)]
    assert [s == State.UNKNOWN for s in states(out)] == want
    assert [o == 0.0 for o, w in zip(out["observed"].to_pylist(), want) if w] == [True] * sum(want)


def test_many_failed_spans_agree_with_the_per_span_definition():
    # (a timing assertion would be flaky; the structure is an as-of join, and what matters here is
    # that thousands of overlapping, unsorted spans give exactly the any-span-contains-it answer)
    import random

    n, rnd = 5000, random.Random(3)
    rows = [(t * STEP, "a", 1.0, 4) for t in range(1, n + 1)]
    spans = [(rnd.randint(1, n), rnd.randint(0, 3)) for _ in range(1500)]
    spans = [(a, a + w) for a, w in spans]
    out = run(rows, failed=[(a * STEP, b * STEP, "x") for a, b in spans], end=n * STEP)
    inside = [any(a <= t <= b for a, b in spans) for t in range(1, n + 1)]
    assert [s == State.UNKNOWN for s in states(out)] == inside


def test_unknown_bucket_is_neither_observed_nor_expected():
    rows = [(t * STEP, "a", 1.0, 4) for t in range(1, 6)]
    out = run(rows, failed=[(2 * STEP, 3 * STEP, "timeout")])
    assert out["observed"].to_pylist() == [4.0, 0.0, 0.0, 4.0, 4.0]
    exp = out["expected"].to_pylist()
    assert exp[1] == exp[2] == 0.0 and min(exp[0], exp[3], exp[4]) > 0


def test_failure_reasons_touch_the_same_buckets_compute_marks_unknown():
    from telemetry_nerd.model.caveats import failure_reasons

    rows = [(t * STEP, "a", 1.0, 4) for t in range(1, 11)]
    failed = [(3 * STEP, 4 * STEP, "x"), (8 * STEP, 8 * STEP, "y")]
    out = run(rows, failed=failed, end=10 * STEP)
    for t, st in zip(out["ts_ms"].to_pylist(), states(out)):
        assert bool(failure_reasons(failed, [t], STEP)) is (st == State.UNKNOWN)


def test_failure_reasons_off_grid_span_and_coarse_buckets_are_wider_on_purpose():
    from telemetry_nerd.model.caveats import failure_reasons

    # a span [a, b] that is not on the grid still reaches the bucket it overlaps ...
    assert failure_reasons([(STEP + 1, STEP + 2, "x")], [2 * STEP], STEP) == ["x"]
    assert failure_reasons([(STEP + 1, STEP + 2, "x")], [STEP, 3 * STEP], STEP) == []
    # ... and a coarse bucket (end 4, covering (0, 4]) is touched by a failure anywhere inside it
    assert failure_reasons([(2 * STEP, 2 * STEP, "x")], [4 * STEP], 4 * STEP) == ["x"]
    assert failure_reasons([(5 * STEP, 5 * STEP, "x")], [4 * STEP], 4 * STEP) == []


# --- mid-series step-rate stretches (bmt): a 16-gap side reaching across a rate change ---


def _fast_between_slow(fast, slow, n, lost=(), slow_first=True):
    """A slow stretch, `n` scrapes `fast` apart (mid-bucket, no spills), and a slow stretch again;
    `lost`: indices into the fast stretch. Returns (states by bucket ts, lost buckets)."""
    pre = list(range(60_000, 15 * 60_000, slow)) if slow_first else []
    t0 = (pre[-1] + slow if pre else 0) + S15 // 2
    fast_ts = [t0 + fast * i for i in range(n)]
    post = list(range(fast_ts[-1] + slow, HOUR, slow))
    gone = {fast_ts[i] for i in lost}
    ts = [t for t in pre + fast_ts + post if t not in gone]
    out = _compute_rows(_count_rows(ts, S15), step=S15, res=S15)
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    return st, {-(-t // S15) * S15 for t in gone}


@pytest.mark.parametrize("slow", [45_000, 60_000])
@pytest.mark.parametrize("back", [2, 3, 5, 8, 12, 16])  # scrapes before the next slow stretch
@pytest.mark.parametrize("side", ["before", "after"])
def test_lost_scrape_in_a_step_rate_stretch_next_to_a_slower_one_mid_series_is_empty(
    slow, back, side
):
    # 15s scrapes for 15 minutes between 45s/60s ones, one lost `back` scrapes from a rate
    # change: the 16 gaps on the slow side of it read slow, and a gap was slower than the step if
    # either side was, so the scrape was hidden (9li fixed only the window edges)
    n = 60
    i = n - 1 - back if side == "before" else back
    st, lost = _fast_between_slow(S15, slow, n, lost=(i,))
    empty = {t for t, s in st.items() if s == State.EMPTY}
    assert empty == lost


@pytest.mark.parametrize("slow_first", [True, False])
def test_two_lost_scrapes_in_a_row_next_to_a_slower_stretch_are_empty(slow_first):
    # a hole (a gap over two steps) between two step-rate runs lies in the stretch
    st, lost = _fast_between_slow(S15, 60_000, 40, lost=(30, 31), slow_first=slow_first)
    assert {t for t, s in st.items() if s == State.EMPTY} == lost


def test_lost_scrape_next_to_a_stretch_twice_as_slow_is_empty():
    # 15s then 30s scrapes: the 30s stretch's consecutive long gaps bound the step-rate run
    st, lost = _fast_between_slow(S15, 30_000, 40, lost=(36,))
    assert {t for t, s in st.items() if s == State.EMPTY} == lost


def test_step_rate_stretch_between_slower_ones_without_loss_has_no_empty():
    for slow in (30_000, 45_000, 60_000):
        st, _ = _fast_between_slow(S15, slow, 40)
        assert State.EMPTY not in st.values(), slow


# --- cadence a little slower than the step (e4v): regular skipped buckets, not losses ---


def _cadence_states(interval, step, *, res, lost=(), phase=3_669, jitter=0, seed=0, end=HOUR):
    ts = _sample_ts(interval, phase=phase, jitter=jitter, seed=seed, end=end)
    gone = {ts[i] for i in lost}
    out = _compute_rows(_count_rows([t for t in ts if t not in gone], step), step=step, end=end,
                        res=res)  # fmt: skip
    return out, {-(-t // step) * step for t in gone}


SLIGHTLY_SLOW = [(16_500, 15_000), (33_000, 30_000), (66_000, 60_000), (15_750, 15_000),
                 (18_000, 15_000), (18_750, 15_000)]  # fmt: skip


@pytest.mark.parametrize(("interval", "step"), SLIGHTLY_SLOW)
def test_slightly_slow_cadence_confirmed_by_the_series_interval_is_ok(interval, step):
    # 16.5s scrapes at a 15s step skip every 11th bucket: read as losses they were 22/240 EMPTY,
    # coverage 0.908. The source's series interval (16.5s) confirms the cadence
    from telemetry_nerd.model.caveats import differing_intervals

    out, _ = _cadence_states(interval, step, res=interval)
    st = states(out)
    assert set(st) <= {State.OK, State.ABSENT}
    assert 0.98 <= _alive_coverage(out) <= 1.02
    [secs] = differing_intervals(out, step, step // 4).values()
    assert abs(secs * 1000 - interval) <= 0.05 * interval


@pytest.mark.parametrize(("interval", "step"), SLIGHTLY_SLOW)
@pytest.mark.parametrize("jitter", [0, 500])
def test_slightly_slow_cadence_without_a_known_interval_is_unknown_not_empty(
    interval, step, jitter
):
    # with the source's interval at the step, a regular skip is a cadence or a loss recurring at
    # that spacing: counts cannot tell, so UNKNOWN + CADENCE, never EMPTY
    out, _ = _cadence_states(interval, step, res=step, jitter=jitter, seed=interval)
    st, fl = states(out), out["flags"].to_pylist()
    assert State.EMPTY not in st and State.PARTIAL not in st
    unknown = [f for s, f in zip(st, fl) if s == State.UNKNOWN]
    assert unknown and all(f & Flag.CADENCE for f in unknown)
    assert 0.98 <= _alive_coverage(out) <= 1.02


@pytest.mark.parametrize("res", [16_500, 15_000])
@pytest.mark.parametrize("lost", [(30,), (31,), (40,), (100, 160), (100, 101)])
def test_lost_scrape_in_a_slightly_slow_series_is_empty(res, lost):
    # 16.5s at 15s skips buckets 11 apart; a lost scrape breaks that spacing around it (31 sits
    # next to a skipped bucket: two 0s in a row)
    out, gone = _cadence_states(16_500, S15, res=res, lost=lost)
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert all(st[b] == State.EMPTY for b in gone)


def test_step_rate_series_losing_scrapes_at_random_is_not_read_as_a_cadence():
    # 1 in ~10 lost at random looks like a 16.5s interval by Σgap / Σsamples, but its 0s are
    # irregular: each is a loss. (Kept 3+ apart: a local cluster losing over a fifth reads as a
    # slower stretch, spec §5.1 limits)
    import random

    rnd = random.Random(5)
    ts = _sample_ts(S15, phase=7_000)
    lost, i = [], 0
    while (i := i + rnd.randint(3, 17)) < len(ts):
        lost.append(i)
    out, gone = _cadence_states(S15, S15, res=S15, phase=7_000, lost=lost)
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert {b for b, s in st.items() if s == State.EMPTY} == gone


def test_scrape_lost_every_eleventh_time_is_unknown_not_ok():
    # undecidable from counts: the same skips as a 16.5s cadence. Never OK (that would hide the
    # loss); UNKNOWN + CADENCE unless the series interval says which
    ts = _sample_ts(S15, phase=7_000)
    out, gone = _cadence_states(S15, S15, res=S15, phase=7_000, lost=range(5, len(ts), 11))
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert {st[b] for b in gone} == {State.UNKNOWN}
    out, _ = _cadence_states(S15, S15, res=16_500, phase=7_000, lost=range(5, len(ts), 11))
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert {st[b] for b in gone} == {State.OK}  # the source says 16.5s: a cadence


def test_slightly_slow_series_that_spills_often_is_at_the_step_rate():
    # 2-sample buckets need jitter over half of (interval - step), which would blur a cadence's
    # spacing: many of them mean a step-rate series, and its regular 0s are losses
    ts = _sample_ts(S15, jitter=2_000, seed=3)
    lost = set(ts[20::23][:10])
    out = _compute_rows(_count_rows([t for t in ts if t not in lost], S15), step=S15, res=S15)
    assert State.UNKNOWN not in states(out)


@pytest.mark.parametrize("seed", range(40))
def test_slightly_slow_cadence_fuzz(seed):
    """Property, over ratios 1.05-1.25, phases, low jitter and loss positions: a loss-free series
    has no EMPTY; every lost scrape's bucket is EMPTY or (where counts cannot tell) UNKNOWN, never
    OK; with the series interval known every lost scrape's bucket is EMPTY."""
    import random

    rnd = random.Random(seed)
    step = rnd.choice([15_000, 30_000, 60_000])
    interval = int(step * rnd.uniform(1.05, 1.25))
    jitter = rnd.choice([0, (interval - step) // 4])
    # a window holding at least LATTICE_SPACINGS + 2 skipped buckets (fewer cannot show a
    # regular spacing: EMPTY, the cautious reading)
    skip = interval / (interval - step)  # buckets between skipped ones
    phase = rnd.randint(0, step)
    end = max(rnd.choice([HOUR, 4 * HOUR]), -(-int(7 * skip * step) // HOUR) * HOUR)
    n = len(_sample_ts(interval, phase=phase, end=end))
    kw = {"phase": phase, "jitter": jitter, "seed": seed, "end": end}
    for res in (step, interval):
        out, _ = _cadence_states(interval, step, res=res, **kw)
        assert State.EMPTY not in states(out), (interval, step, res)
        lost = rnd.sample(range(2, n - 2), rnd.randint(1, 3))
        out, gone = _cadence_states(interval, step, res=res, lost=lost, **kw)
        st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
        allowed = {State.EMPTY} if res == interval else {State.EMPTY, State.UNKNOWN}
        assert {st[b] for b in gone} <= allowed, (interval, step, res, lost)


# --- review of bmt/e4v: consumers of a CADENCE unknown, loss-sample-loss, assumed intervals ---


def _cadence_pair(end=6 * HOUR):
    """16.5s scrapes ('a', interval unknown) and 15s ones ('b') at a 15s step."""
    rows = _count_rows(_sample_ts(16_500, phase=3_669, end=end), S15, "a")
    rows += _count_rows(_sample_ts(S15, phase=3_669, end=end), S15, "b")
    return compute(buckets(rows), ("a", "b"), start_ms=S15, end_ms=end, step_ms=S15,
                   resolution_ms=S15, mode="samples")  # fmt: skip


@pytest.mark.parametrize("k", [4, 20])
def test_coarsening_a_cadence_unknown_is_partial_not_unknown(k):
    # "OK or one lost scrape": a coarse bucket that saw samples is PARTIAL (flag kept), not
    # UNKNOWN for the whole 1m / 5m
    from telemetry_nerd.model.bucket_state import coarsen

    out = coarsen(_cadence_pair(), k * S15)
    a = [(s, f) for s, f, i in zip(states(out), out["flags"].to_pylist(),
                                   out["series_id"].to_pylist()) if i == "a"]  # fmt: skip
    assert State.UNKNOWN not in {s for s, _ in a}
    assert all(s == State.PARTIAL for s, f in a if f & Flag.CADENCE)
    assert any(f & Flag.CADENCE for _, f in a)


def test_merging_a_cadence_unknown_member_is_partial_not_unknown():
    from telemetry_nerd.model.bucket_state import merge

    out = merge(_cadence_pair(), {"a": "g", "b": "g"})
    st, fl = out["state"].to_pylist(), out["flags"].to_pylist()
    assert State.UNKNOWN not in st
    assert all(s == State.PARTIAL for s, f in zip(st, fl) if f & Flag.CADENCE)


def test_coarse_bucket_of_only_cadence_unknowns_and_failures_stays_unknown():
    # nothing seen and a cadence unknown: still nothing known
    from telemetry_nerd.model.bucket_state import coarsen

    fine = _cadence_pair()
    df = [r for r in fine.to_pylist() if r["series_id"] == "a"]
    cad = next(r["ts_ms"] for r in df if r["flags"] & Flag.CADENCE)
    one = pa.Table.from_pylist([r for r in df if r["ts_ms"] == cad], schema=fine.schema)
    assert states(coarsen(one, 4 * S15)) == [State.UNKNOWN]


def test_claim_over_a_cadence_unknown_series_warns_with_its_reason():
    from telemetry_nerd.core.coverage_check import claim_coverage

    out = _cadence_pair()
    a = out.filter(pa.compute.equal(out["series_id"], "a"))
    [c] = claim_coverage(a, HOUR, 3 * HOUR, S15)
    assert c.severity == "warn" and c.code == "missing_data"
    assert "cadence" in c.message and "a little slower than the query step" in c.message
    # several series: the cadence one is not 'untrusted'
    [c] = claim_coverage(out, HOUR, 3 * HOUR, S15)
    assert c.severity == "warn" and "could not return" not in c.message


def _cadence_change(fast_i, slow_i, n, lost, *, fast_first=True):
    """`n` scrapes `fast_i` apart, then (or before) `slow_i` ones over 3 hours, mid-series (a
    slow stretch on both sides); `lost`: indices into the fast stretch."""
    a, end = HOUR, 3 * HOUR
    ts, t = [], 7_000
    while t <= end:
        ts.append(t)
        t += fast_i if a <= t < a + n * fast_i else slow_i
    fast = [i for i, x in enumerate(ts) if a <= x < a + n * fast_i]
    gone = {ts[fast[i]] for i in lost}
    out = _compute_rows(_count_rows([x for x in ts if x not in gone], S15), step=S15, end=end,
                        res=S15)  # fmt: skip
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    return st, {-(-x // S15) * S15 for x in gone}


@pytest.mark.parametrize("pos", [2, 5, 9, 14, 20, 30, 33])
@pytest.mark.parametrize("slow", [45_000, 60_000])
def test_loss_sample_loss_next_to_a_slower_stretch_is_empty(pos, slow):
    # scrapes i and i+2 lost: two long gaps in a row (30s, 30s), cut as a slower stretch, so the
    # 16-gap sides across the change hid both (52/68 hidden, as on master)
    st, gone = _cadence_change(S15, slow, 40, (pos, pos + 2))
    assert {st[b] for b in gone} == {State.EMPTY}


def test_lost_scrape_next_to_a_skip_of_a_slightly_slow_stretch_before_a_slower_one_is_empty():
    # a 1.1x stretch (66s at a 60s step) before 180s scrapes, two lost: the one next to a
    # skipped bucket gives two long gaps in a row; read as a slower stretch it was hidden, where
    # master showed it (review seed 1107)
    ts, t = [], 167
    while t <= HOUR:
        ts.append(t)
        t += 66_000 if t < 1_890_000 else 180_000
    ts = ts[1:]  # (the first one falls before the window)
    gone = {ts[17], ts[26]}
    out = _compute_rows(_count_rows([x for x in ts if x not in gone], 60_000), step=60_000,
                        res=66_000)  # fmt: skip
    st = dict(zip(out["ts_ms"].to_pylist(), out["state"].to_pylist()))
    assert st[-(-ts[17] // 60_000) * 60_000] == State.EMPTY


def test_an_assumed_series_interval_does_not_confirm_a_cadence():
    # the source's resolution is a default (origin "assumed"): it cannot say which, UNKNOWN
    ts = _sample_ts(16_500, phase=3_669)
    rows = _count_rows(ts, S15)
    kw = {
        "start_ms": S15,
        "end_ms": HOUR,
        "step_ms": S15,
        "resolution_ms": 16_500,
        "mode": "samples",
    }
    known = compute(buckets(rows), ("a",), **kw, interval_known=True)
    assumed = compute(buckets(rows), ("a",), **kw, interval_known=False)
    assert State.UNKNOWN not in states(known) and State.UNKNOWN in states(assumed)


@pytest.mark.parametrize(("origin", "known"), [("configured", True), ("learned", True),
                                               ("assumed", False), (None, False)])  # fmt: skip
def test_only_a_configured_or_learned_interval_is_recorded_as_known(origin, known):
    from types import SimpleNamespace

    from telemetry_nerd.core.service import _semantics_flags
    from telemetry_nerd.datasets.store import DatasetMeta
    from telemetry_nerd.model.companions import derive_states
    from telemetry_nerd.model.series import SERIES_SCHEMA, FetchResult

    src = SimpleNamespace(semantics=None, resolution_origin=origin)
    flags = _semantics_flags(src)
    assert bool(flags.get("series_interval_known")) is known
    rows = _count_rows(_sample_ts(16_500, phase=3_669), S15)
    meta = DatasetMeta(id="d", source="s", expr="up", start_ms=S15, end_ms=HOUR, step_ms=S15,
                       resolution_ms=16_500, semantics_flags=flags)  # fmt: skip
    series = pa.table({"series_id": ["a"], "labels": ["{}"]}, schema=SERIES_SCHEMA)
    st = states(derive_states(meta, FetchResult(buckets(rows), series)))
    assert (State.UNKNOWN in st) is not known


# --- review round 2: a slower stretch next to a step-rate one is not absorbed into its run ---


@pytest.mark.parametrize("ratio", [1.33, 1.5, 1.67, 1.8, 2.0])
@pytest.mark.parametrize("step", [S15, 60_000])
@pytest.mark.parametrize("slow_first", [True, False])
def test_slower_stretch_next_to_a_step_rate_one_keeps_its_cadence(ratio, step, slow_first):
    # 1.5 h at ratio x the step, 2.5 h at the step, loss-free: judged once over the whole run,
    # a 1.5x stretch (isolated long gaps) or 1.67x one (bridged pairs) joined the step-rate run
    # and every skip read EMPTY (120 of them for 1.5x at 15s)
    end, interval = 4 * HOUR, int(step * ratio)
    ts, t = [], 3_001
    while t < end:
        ts.append(t)
        slow = t < 1.5 * HOUR if slow_first else t >= 2.5 * HOUR
        t += interval if slow else step
    out = _compute_rows(_count_rows(ts, step), step=step, end=end, res=step)
    # at most the last few skips next to the step-rate stretch, whose 16 gaps on that side are at
    # the step's rate: where the change lies is ambiguous within a few samples
    assert states(out).count(State.EMPTY) <= 4


def test_coarse_cadence_partial_names_its_reason():
    # the 5m caveat said only "fewer samples than expected"
    from telemetry_nerd.model.bucket_state import coarsen
    from telemetry_nerd.model.caveats import CADENCE_REASON, from_bucket_state

    out = coarsen(_cadence_pair(), 20 * S15)
    [c] = [c for c in from_bucket_state(out, {"a": "A", "b": "B"}, 20 * S15)
           if c.code == "missing_data"]  # fmt: skip
    assert CADENCE_REASON in c.message
