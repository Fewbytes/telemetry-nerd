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


def _compute_rows(rows, *, step, end=HOUR, failed=(), res=None):
    return compute(
        buckets(rows),
        ("a",),
        start_ms=step,
        end_ms=end,
        step_ms=step,
        resolution_ms=res or step,
        mode="samples",
        failed=failed,
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
    for interval in (15_000, 20_000, 25_000, 40_000, 60_000, 120_000)
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
def test_rate_change_within_the_window_reads_ok_and_flags_interval_change(fast, slow):
    half = HOUR // 2
    ts = _sample_ts(fast, end=half) + _sample_ts(slow, start=half, end=HOUR)
    out = _compute_rows(_count_rows(ts, S15), step=S15, res=S15)
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
    assert f"{first // 1000}s → {then // 1000}s" in c.message


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
