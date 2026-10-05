"""Little's law check, statistics layer (czt.2, 60j): seeded M/M/c simulations.

Measured on littles_sim (150 seeds; FAR = verdict not consistent on consistent sims): before 60j
(Poisson count errors in the interval) pointwise coverage 98.3-100%, FAR 0-2%; after (measurement
terms only) coverage 97.3-100%, FAR 0-2%; a 1.05x gauge offset at lambda=9.5 found 19% of the
time (5% before); a 5-minute load spike out of steady state is transient at exactly its windows
75/75 (0/75 before). Load-peak promotion (83w, q2m; 75 seeds): the same spike with an arrivals
counter is special cause at its peak 75/75 (1/75 before), with a completions counter 75/75 (49/75
before: the rest common cause); consistent traffic, rho 0.95, load steps and low traffic: no
promotion. Table: docs/superpowers/specs/2026-10-02-littles-law-design.md."""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from telemetry_nerd.analysis.littles import (
    COMMON,
    MEASUREMENT,
    PEAK_COMMON,
    SPECIAL,
    UNDETERMINED,
    Substeps,
    _cause,
    _sampling_factor,
    cantelli_k,
    check,
    combine,
    judge,
    kendall_increasing_p,
)

from .littles_sim import simulate, substeps

LOAD = [(0.0, 2.0), (1200.0, 3.7), (2400.0, 2.0)]  # M/M/4, mu=1: rho 0.5 -> 0.925 -> 0.5
SPIKE = [(0.0, 2.0), (1500.0, 5.0), (1800.0, 2.0)]  # M/M/4: rho 1.25 for 5 min, a queue builds


def test_consistent_mmc_false_alarms_at_most_nominal():
    """Timer from arrival: L = lambda W holds; the overall verdict (5% by construction) must not
    alarm more often than that, and pointwise 95% measurement intervals must cover 1 at least
    95% of the time. The discrepancy is reported whatever the verdict."""
    n, alarms, covered, windows = 60, 0, 0, 0
    for seed in range(n):
        r = check(substeps(simulate(seed, rates=[(0.0, 2.0)])), k=20)
        alarms += r.verdict != "consistent"
        assert bool(r.systematic or r.transient) == (r.verdict != "consistent")
        assert not r.promoted  # steady state: no evidence of leaving it
        for w in r.windows:
            windows += 1
            covered += w.ci95[0] <= 1 <= w.ci95[1]
            assert w.diff_ci[0] <= w.diff <= w.diff_ci[1]
            assert math.isclose(w.diff, w.L - w.lambda_W)
    # binomial(60, 0.05): P(>= 7 alarms) < 2%
    assert alarms <= 6
    assert covered / windows >= 0.95


def test_consistent_under_heavy_load_and_high_concurrency():
    alarms = 0
    for seed in range(20):
        r = check(substeps(simulate(100 + seed, rates=[(0.0, 19.0)], c=25)), k=20)
        alarms += r.verdict != "consistent"
        assert r.common_cause["warning"] is None  # 5700 requests per window: +-5%
    assert alarms <= 2


def test_measurement_interval_holds_no_count_noise_and_the_common_cause_scale_is_apart():
    """Over a window L and lambda W describe the same requests: the counts' Poisson noise is the
    common-cause scale, not an error of the comparison; the interval is the instruments' only."""
    b = check(substeps(simulate(3, rates=[(0.0, 9.5)], c=10)), k=20).windows[2]
    assert set(b.sd_terms) == {"gauge_sampling", "edge_straddle", "scrape_timing"}
    assert set(b.bias) == {"alignment"}
    q = (b.ci95[1] - b.ratio) / b.sd  # the t quantile used
    assert math.isclose(
        b.common, q * (1 / math.sqrt(b.arrivals) + 1 / math.sqrt(b.completions)), rel_tol=1e-9
    )


def test_low_traffic_warns_with_the_common_cause_scale_and_still_shows_the_discrepancy():
    alarms = 0
    for seed in range(40):
        r = check(substeps(simulate(200 + seed, rates=[(0.0, 0.3)])), k=20)
        alarms += r.verdict != "consistent"
        cc = r.common_cause
        n = cc["completions_per_window"]
        assert 60 < n < 130  # 0.3/s x 300 s
        assert 0.3 < cc["rel95"] < 0.6  # about 2 x 1.96 / sqrt(90)
        assert f"±{100 * cc['rel95']:.0f}%" in cc["warning"]
        assert f"N≈{n:.0f}" in cc["warning"]
        assert all(w.diff is not None and w.ratio is not None for w in r.windows)
    assert alarms <= 4


@pytest.mark.parametrize("seed", range(5))
def test_unmeasured_queueing_under_load_is_transient_at_the_peak(seed):
    """The timer starts at service start: queueing is in L, not in W. Only the loaded third
    (20-40 min) queues materially: transient windows there (plus one of drain), at the load
    peak, beyond the common-cause envelope; the unloaded windows are not transient."""
    r = check(substeps(simulate(seed, rates=LOAD, timer="service_start")), k=20)
    assert r.verdict in ("L_high", "inconsistent_in_windows")
    assert r.transient, "some loaded window is transient"
    for t in r.transient:
        w = r.windows[t["index"]]
        assert t["direction"] == "L_high" and 1200_000 <= w.start_ms < 2700_000
    assert any(t["at_peak"] and t["source"] == SPECIAL for t in r.transient)
    assert all(w.source == MEASUREMENT for w in r.windows[:4])


@pytest.mark.parametrize("seed", range(4))
def test_unmeasured_queueing_at_constant_load_is_systematic(seed):
    """rho 0.95 all hour: every window carries the hidden queue: a systematic offset (the
    measurement system), with most windows at its level."""
    r = check(substeps(simulate(700 + seed, rates=[(0.0, 9.5)], c=10, timer="service_start")), k=20)
    assert r.verdict == "L_high"
    assert r.systematic is not None and r.systematic.verdict == "L_high"
    assert 1.8 < r.systematic.ratio < 3.5 and r.systematic.ci95[0] > 1
    assert 2 * len(r.core) > len(r.windows)
    assert r.reference == r.systematic.ratio


@pytest.mark.parametrize("seed", range(6))
def test_a_load_spike_out_of_steady_state_is_transient_at_its_windows(seed):
    """rho 1.25 for 5 min (25-30 min): a queue builds, then drains. The counter counts
    completions (most middleware): in the peak window L holds in-flight time that completed
    latencies do not show yet (L_high), in the drain window completions carry time from before
    it (L_low). Transient exactly there, no systematic offset."""
    r = check(substeps(simulate(600 + seed, rates=SPIKE, counter="completions")), k=20)
    assert r.verdict == "inconsistent_in_windows" and r.systematic is None
    by = {t["index"]: t for t in r.transient if "grid" not in t}
    assert set(by) == {5, 6}
    # the shifted grid adds only windows inside the episode (25-30 min and its drain)
    for t in r.transient:
        if t.get("grid") == "offset":
            assert 1350_000 <= t["block"].start_ms <= 1950_000, t
    assert by[5]["direction"] == "L_high" and by[5]["phase"] == "peak" and by[5]["at_peak"]
    assert by[6]["direction"] == "L_low" and by[6]["phase"] == "drain"
    assert by[5]["load"]["backlog_change"] > 50 and by[6]["load"]["backlog_change"] < -50


def test_one_heavy_window_does_not_make_an_offset_systematic():
    """The spike with an arrivals counter: the instruments nearly agree; the drain window holds
    30x the traffic of the others, and must not turn into a 'systematic' offset."""
    for seed in range(10):
        r = check(substeps(simulate(600 + seed, rates=SPIKE)), k=20)
        assert r.systematic is None


def test_a_change_confined_to_some_windows_off_peak_is_special_cause():
    m = simulate(11, rates=[(0.0, 6.0)], c=10)
    sub = substeps(m)
    conc = sub.conc.copy()
    conc[60:80] *= 1.6  # window 3 only: the gauge briefly counts more (a deploy, say)
    shifted = Substeps(
        sub.ts_ms, sub.arrivals, sub.lat_sum, sub.lat_count, conc, sub.conc_n, sub.step_ms
    )
    # the main grid alone (the change fills its window 3 exactly)
    r = check(shifted, k=20, offset=False)
    assert r.verdict == "inconsistent_in_windows"
    (t,) = r.transient
    assert t["index"] == 3 and t["phase"] == "other" and t["source"] == SPECIAL
    assert r.windows[3].source == SPECIAL


def test_a_change_straddling_two_windows_is_found_on_the_shifted_grid():
    """xa4: a change filling the second half of one window and the first half of the next is
    diluted in both; the grid shifted by half a window holds it whole (Bonferroni over both)."""
    m = simulate(11, rates=[(0.0, 6.0)], c=10)
    sub = substeps(m)
    conc = sub.conc.copy()
    conc[70:90] *= 1.8  # windows 3 and 4 half each; the shifted grid's window 3 whole
    s2 = Substeps(sub.ts_ms, sub.arrivals, sub.lat_sum, sub.lat_count, conc, sub.conc_n,
                  sub.step_ms)  # fmt: skip
    assert check(s2, k=20, offset=False).verdict == "consistent"
    r = check(s2, k=20)
    assert r.verdict == "inconsistent_in_windows"
    (t,) = r.transient
    assert t["grid"] == "offset" and t["index"] is None and t["source"] == SPECIAL
    assert (t["block"].start_ms, t["block"].end_ms) == (sub.ts_ms[69], sub.ts_ms[89])
    assert r.offset is not None and r.offset.windows[3].source == SPECIAL


def test_queueing_measured_by_the_timer_is_consistent():
    r = check(substeps(simulate(3, rates=LOAD, timer="arrival")), k=20)
    assert r.verdict == "consistent"


def test_missing_instance_makes_the_total_systematically_low():
    sims = [simulate(10 + i, rates=[(0.0, 6.0)], c=10) for i in range(3)]
    parts = [substeps(m, drop_gauge=(i == 2)) for i, m in enumerate(sims)]
    total = check(combine(parts), k=20)
    assert total.verdict == "L_low"
    assert total.systematic is not None and total.systematic.ci95[1] < 0.8  # ~2/3 in the gauge
    assert not total.transient and len(total.core) == len(total.windows)
    assert total.pooled.ci95[1] < 0.8
    whole = check(combine([substeps(m) for m in sims]), k=20)
    assert whole.verdict == "consistent"


def test_leaked_requests_grow():
    """A leak: a persistent, drifting offset (measurement system), not a run of transients."""
    m = simulate(7, rates=[(0.0, 4.0)], c=8)
    m["gauge"] = m["gauge"] + np.floor(m["ts"] / 120)  # one stuck request every 2 minutes
    r = check(substeps(m), k=20)
    assert r.verdict == "L_high"
    assert r.growing and r.growing["growing"]
    assert r.systematic is not None and not r.transient
    assert r.references[11] > r.references[0]  # the reference follows the drift


def test_no_traffic_is_not_judged():
    ts = np.arange(1, 41, dtype=np.int64) * 15_000
    z = np.zeros(40)
    sub = Substeps(ts, z, z, z, np.ones(40), np.ones(40), 15_000)
    b = judge(sub, np.arange(40))
    assert b.verdict == "no_traffic"
    assert "in_flight_without_traffic" in b.flags


def test_too_few_substeps_is_insufficient():
    m = simulate(1, duration_s=60)
    assert judge(substeps(m), np.arange(3)).verdict == "insufficient"


def test_constant_gauge_still_gets_the_sampling_floor():
    """A gauge that reads the same at every scrape (spikes in between) must not give a zero-width
    interval: the Poisson-occupancy floor applies."""
    ts = np.arange(1, 41, dtype=np.int64) * 15_000
    lam = np.full(40, 2.0)
    sub = Substeps(ts, lam, lam * 0.5, lam, np.full(40, 1.0), np.ones(40), 15_000)
    b = judge(sub, np.arange(40))
    assert b.sd_source == "floor"
    assert b.ci95[1] - b.ci95[0] > 0.2


def test_systematic_sampling_factor_limits():
    assert _sampling_factor(1000.0, 1.0) == 1.0  # independent samples
    assert math.isclose(_sampling_factor(1e-4, 1.0), 1e-4 / 6)
    assert 0 < _sampling_factor(1.0, 1.0) < 1


def test_flow_balance_flags_a_counter_counting_more():
    m = simulate(4, rates=[(0.0, 5.0)], c=10)
    sub = substeps(m)
    doubled = Substeps(
        sub.ts_ms, sub.arrivals * 1.5, sub.lat_sum, sub.lat_count, sub.conc, sub.conc_n,
        sub.step_ms,
    )  # fmt: skip
    r = check(doubled, k=20)
    assert "flow_imbalance" in r.pooled.flags
    assert "flow_imbalance" not in check(sub, k=20).pooled.flags


# --- load-peak promotion (83w, q2m): option C ---------------------------------------------------
OVERLOAD = [(0.0, 2.0), (1200.0, 4.2), (2100.0, 2.0)]  # rho 1.05 for 15 min (windows 4-6)


def _repeated(lams):
    """Peaks of 5 min every 20 min over 2 h, rho 0.5 between them."""
    out = [(0.0, 2.0)]
    for j, lam in enumerate(lams):
        out += [(1200.0 * j + 600, lam), (1200.0 * j + 900, 2.0)]
    return out


def test_promotion_tools():
    assert math.isclose(cantelli_k(0.05), math.sqrt(19))  # 1 / (1 + k^2) = alpha
    assert kendall_increasing_p([1, 2, 3]) == (3, 1 / 6)
    assert kendall_increasing_p([1, 2, 3, 4, 5]) == (10, 1 / 120)
    s, p = kendall_increasing_p([2, 1, 3, 4, 5])  # one inversion: 1 + 4 orders of 120
    assert s == 8 and math.isclose(p, 5 / 120)
    assert kendall_increasing_p([1, 1, 1])[1] == 1.0  # ties count against the trend


@pytest.mark.parametrize("seed", range(6))
def test_spike_with_an_arrivals_counter_is_special_cause_at_the_peak(seed):
    """q2m: with an arrivals counter the instruments partly compensate (L ~ lambda W in the peak
    window), but the backlog grows by hundreds: promoted to special cause, saying why."""
    r = check(substeps(simulate(600 + seed, rates=SPIKE)), k=20, arrivals="arrivals")
    w = r.windows[5]
    assert w.source == SPECIAL
    if w.promotion is None:  # beyond the envelope already (seed 605)
        assert [t["source"] for t in r.transient if t["index"] == 5] == [SPECIAL]
        return
    (p,) = [p for p in r.promoted if p["index"] == 5]
    assert p["from"] in (COMMON, MEASUREMENT)
    (e,) = [e for e in p["evidence"] if e["kind"] == "backlog_growth"]
    assert e["significant"] and e["gauge"] > 200 and e["flow"] > 200 and e["flow_used"]
    assert e["z"] >= e["k"] and e["interval"][0] <= e["value"] <= e["interval"][1]
    assert e["value"] == min(e["gauge"], e["flow"])
    assert "backlog grew" in p["reason"] and "Cantelli" in p["reason"]
    assert [q["index"] for q in r.promoted] == [5]  # nothing else promoted


@pytest.mark.parametrize("seed", range(6))
def test_spike_with_a_completions_counter_is_special_cause_at_the_peak(seed):
    """83w: inside the envelope the peak window used to stay common cause while the warning said
    'transition out of steady state'; with a growing backlog it is promoted."""
    r = check(substeps(simulate(600 + seed, rates=SPIKE, counter="completions")), k=20)
    (t,) = [t for t in r.transient if t["index"] == 5]
    assert t["source"] == SPECIAL and t["at_peak"]
    assert t["promoted"] == (seed == 2)  # 602: the 83w case; the others beyond the envelope
    if t["promoted"]:
        assert t["promotion"]["from"] == COMMON
        assert t["cause"].startswith("at a load peak, leaving steady state (promoted")
        # completions counter: the counter is the latency count, no flow reading
        (e,) = [e for e in t["promotion"]["evidence"] if e["kind"] == "backlog_growth"]
        assert e["flow"] is None and e["significant"]


@pytest.mark.parametrize("seed", [10, 29])  # seeds with such a peak (11/50 of seeds 0-49)
def test_a_load_peak_inside_the_envelope_without_evidence_stays_common_cause(seed):
    """rho 0.95 with queueing the timer misses: heavy-tailed excursions put some windows at a
    'peak' (W well above the median) with a deviation inside the envelope; no backlog growth or
    consecutive W rise: common cause, worded as not a signal by itself."""
    r = check(substeps(simulate(seed, rates=[(0.0, 9.5)], c=10, timer="service_start")), k=20)
    peaks = [t for t in r.transient if t["at_peak"] and t["source"] == COMMON]
    assert peaks and not r.promoted
    for t in peaks:
        assert t["cause"] == PEAK_COMMON and not t["promoted"]
        assert r.windows[t["index"]].source == COMMON


def test_no_promotion_in_steady_state_heavy_load_steps_or_low_traffic():
    """Promotion has its own 5% family-wise budget: steady state at rho 0.95 (heavy-tailed
    excursions), a step to a higher steady load (W rises once, then holds) and a small system
    must not be promoted."""
    cases = [
        *[{"rates": [(0.0, 9.5)], "c": 10, "seed": 40 + s} for s in range(15)],
        *[{"rates": LOAD, "seed": 60 + s} for s in range(15)],
        *[{"rates": [(0.0, 0.3)], "seed": 80 + s} for s in range(15)],
    ]
    promoted = 0
    for kw in cases:
        r = check(substeps(simulate(**kw)), k=20, arrivals="arrivals")
        promoted += bool(r.promoted)
        # 12 windows on each of the two grids (main and shifted by half a window)
        assert r.promotion["alpha"] == 0.05 and r.promotion["tests"] == 24
    assert promoted <= 1


@pytest.mark.parametrize("seed", [1, 7, 10])
def test_overload_with_w_rising_across_consecutive_windows_is_promoted(seed):
    """rho 1.05 for 15 min: W climbs window after window; (b) promotes on two consecutive rises,
    each beyond its t threshold, with the path of W in the reason."""
    r = check(substeps(simulate(800 + seed, rates=OVERLOAD)), k=20, arrivals="arrivals")
    lr = [
        (p, e) for p in r.promoted for e in p["evidence"]
        if e["kind"] == "latency_rise" and e["significant"]
    ]  # fmt: skip
    assert lr
    for p, e in lr:  # on either grid (the shifted one: half a window later)
        start = p["block"].start_ms if p.get("grid") else r.windows[p["index"]].start_ms
        assert 1_200_000 <= start <= 1_950_000
        a, b, c = e["W"]
        assert a < b < c and all(z >= k for z, k in zip(e["z"], e["k"], strict=True))
        assert "rose across consecutive windows" in p["reason"]


@pytest.mark.parametrize("seed", [7, 22, 44])
def test_deviation_growing_across_repeated_peaks_is_promoted(seed):
    """Six load peaks, rho 0.6 -> 0.9, queueing the timer misses: each peak's deviation is
    inside its interval or envelope, but it grows peak after peak (Kendall's S, exact p). Low
    power by design (6 points, distribution-free): 8/50 of seeds 900-949 promote (10/50 before
    the gauge's end-of-interval reading was corrected, part of which was that bias)."""
    m = simulate(
        900 + seed, rates=_repeated([2.4, 2.64, 2.88, 3.12, 3.36, 3.6]), duration_s=7200.0,
        timer="service_start", counter="completions",
    )  # fmt: skip
    r = check(substeps(m), k=20, arrivals="completions")
    peaks = r.promotion["peaks"]
    assert len(peaks["windows"]) == 6 and peaks["p"] <= r.promotion["alpha_peaks"]
    assert r.promoted and all(p["index"] in peaks["windows"][1:] for p in r.promoted)
    for p in r.promoted:
        (e,) = [e for e in p["evidence"] if e["kind"] == "peak_growth"]
        assert e["significant"] and e["deviation"] > e["deviation_first"]
        assert "repeated load peaks" in p["reason"] and "Kendall" in p["reason"]


def test_gauge_end_of_interval_reading_is_corrected_to_the_trapezoid():
    """The counters cover (s_j-1, s_j], the gauge is read at s_j: summing the readings integrates
    N half a scrape late. For a linear path the trapezoid-corrected L is the exact time average
    over the window (the correction has a known sign: corrected, not added to the interval)."""
    n, step = 60, 15_000
    ts = np.arange(1, n + 1, dtype=np.int64) * step
    N = 0.02 * ts / 1000  # a backlog building linearly: 0.3 per scrape
    one = np.ones(n)
    sub = Substeps(ts, 5 * one, 5 * one, 5 * one, N, one, step)
    idx = np.arange(20, 40)
    b = judge(sub, idx)
    exact = (N[19] + N[39]) / 2  # the time average of a linear N over (s_19, s_39]
    assert math.isclose(b.L, exact, rel_tol=1e-12)
    assert math.isclose(b.L_correction, -(N[39] - N[19]) * 15 / 2 / (20 * 15))
    assert b.bias == {"alignment": 0.0}


# --- 4ahp: fewer than MIN_SPREAD windows: the special-cause label rests on the cautious envelope
def _steady(n: int = 80, lam: float = 10.0) -> dict[str, np.ndarray]:
    """Noise-free steady traffic: lambda = 10/s, W = 1 s, L = 10 (Little's law exact)."""
    return {
        "arrivals": np.full(n, lam), "lat_sum": np.full(n, lam), "lat_count": np.full(n, lam),
        "conc": np.full(n, lam),
    }  # fmt: skip


def _sub(d: dict[str, np.ndarray]) -> Substeps:
    n = d["arrivals"].size
    ts = (np.arange(1, n + 1) * 15_000).astype(np.int64)
    return Substeps(ts, d["arrivals"], d["lat_sum"], d["lat_count"], d["conc"], np.ones(n), 15_000)


def test_few_windows_a_deviation_beyond_the_cautious_envelope_is_special_cause():
    d = _steady()
    d["conc"][40:60] *= 1.5  # window 2 of 4: L 50% above lambda W
    r = check(_sub(d), k=20, offset=False)
    w = r.windows[2]
    assert w.source == SPECIAL and w.common_cautious == pytest.approx(w.common)
    (t,) = r.transient
    assert t["envelope"]["label_rests_on"] == "cautious" and t["envelope"]["spread"] is None
    assert r.common_cause["envelope"] == "cautious" and r.common_cause["spread_rel"] == 0.0


def test_few_windows_beyond_the_poisson_envelope_only_is_undetermined():
    """Arrivals in bursts inside the window (0 and 20 per second, sub-step to sub-step): the
    Poisson envelope (~8%) is far below the deviation, the cautious one (the window's own
    arrival dispersion) above it: no estimate of the process's own variation from 4 windows, so
    no special-cause label (principle 16), both envelopes reported."""
    d = _steady()
    d["conc"][40:60] *= 1.5
    d["arrivals"][40:60] = np.where(np.arange(20) % 2 == 0, 0.0, 20.0)
    r = check(_sub(d), k=20, offset=False)
    w = r.windows[2]
    assert w.source == UNDETERMINED
    env = r.transient[0]["envelope"]
    assert env["small_system"] < env["deviation"] < env["cautious"]
    assert w.common_terms["k_arrivals"] > 5 and w.common_terms["k_latency"] == 1.0
    assert "undetermined" in _cause({**r.transient[0], "phase": "other"})


def test_with_five_windows_the_spread_still_decides():
    d = _steady(n=100)
    d["conc"][40:60] *= 1.5
    d["arrivals"][40:60] = np.where(np.arange(20) % 2 == 0, 0.0, 20.0)
    r = check(_sub(d), k=20, offset=False)
    assert r.windows[2].source == SPECIAL  # MIN_SPREAD windows: the Poisson / spread envelope
    assert r.common_cause["envelope"] == "small_system_or_spread"


def _recorded(name: str) -> tuple[Substeps, dict, dict]:
    """A Substeps recorded from check_littles_law on VictoriaMetrics (None = NaN)."""
    path = Path(__file__).parents[1] / "fixtures" / "littles" / f"{name}.json"
    rec = json.loads(path.read_text())
    s = rec["substeps"]
    arr = {k: np.array([math.nan if x is None else x for x in v], float)
           for k, v in s.items() if isinstance(v, list) and k != "ts_ms"}  # fmt: skip
    sub = Substeps(ts_ms=np.array(s["ts_ms"], np.int64), **arr,
                   **{k: v for k, v in s.items() if not isinstance(v, list)})  # fmt: skip
    return sub, rec["call"], rec


def test_a_load_peak_window_does_not_make_a_systematic_offset():
    """1i26 (CI, integration overload_spike / arrivals, a placement 60 ms past a tile edge): the
    load-peak window (backlog building, not steady: its exact R 1.075 is the process leaving
    steady state, not the instruments) sat in the level of the non-transient windows with five
    steady ones whose gauge sampling read 0-9% high, and the level's interval cleared 1: an
    `L_high` systematic offset (measurement system) on a simulation whose instruments agree
    (exact R of the steady windows 0.994-1.005). Little's law over a window holds only in
    steady state, so a measurement offset is judged on the steady windows when they are most."""
    sub, call, rec = _recorded("overload_spike_arrivals_1i26")
    r = check(sub, **call)
    assert r.systematic is None and r.verdict == "inconsistent_in_windows", r.verdict
    (t,) = [t for t in r.transient if "grid" not in t]
    assert t["index"] == 3 and t["phase"] == "drain" and t["direction"] == "L_low"
    # every window's measurement interval still holds the exact R
    for w, exact in zip(r.windows, rec["exact_R"], strict=True):
        assert w.ci95[0] <= exact <= w.ci95[1]


@pytest.mark.parametrize("k", [12, 24])
def test_the_recorded_spike_makes_no_systematic_offset_on_any_grid(k):
    """1i26: the same recorded data on every grid offset (skip 0..k) at 1- and 2-minute windows:
    the episode moves between windows, its not-steady windows never make a measurement-system
    offset (before the fix: k=24 skip=1 and k=12 skip=7 read L_high)."""
    sub, call, _ = _recorded("overload_spike_arrivals_1i26")
    for skip in range(k + 1):
        r = check(sub, k=k, skip=skip, tests=call["tests"], arrivals=call["arrivals"])
        assert r.systematic is None and not r.basis, (skip, r.verdict)
        assert r.verdict in ("inconsistent_in_windows", "consistent"), (skip, r.verdict)


def _padded_with_quiet_baseline(name: str, reps: int = 3) -> tuple[Substeps, dict]:
    """`_recorded`'s substeps with `reps` extra copies of its first two windows (a quiet stretch,
    well before the spike) on each side: a baseline away from the spike's episode
    (`_backlog_not_steady`'s MIN_BASELINE) without touching the recorded spike itself."""
    sub, call, _ = _recorded(name)
    quiet = 2 * call["k"]
    fields = ("arrivals", "lat_sum", "lat_count", "conc", "conc_n")
    ext = {
        f: np.concatenate(
            [getattr(sub, f)[:quiet]] * reps + [getattr(sub, f)] + [getattr(sub, f)[:quiet]] * reps
        )
        for f in fields
    }  # fmt: skip
    n = ext["arrivals"].size
    ts = (np.arange(1, n + 1) * sub.step_ms).astype(np.int64)
    padded = Substeps(
        ts, step_ms=sub.step_ms, lookback_ms=sub.lookback_ms, scrape_ms=sub.scrape_ms, **ext
    )
    return padded, call


def test_a_drain_the_linear_trend_test_misses_is_caught_against_its_own_baseline():
    """cgk3 (1i26 follow-up): the recorded drain (L 481 -> 19, -396% relative change) reads
    not_steady False from the within-window linear trend test alone (under n_eff: the bug) --
    padded with quiet windows for a baseline (MIN_BASELINE), `_backlog_not_steady` catches it
    instead: the change is far beyond what the quiet windows' own gauge variance explains."""
    sub, call = _padded_with_quiet_baseline("overload_spike_arrivals_1i26")
    r = check(
        sub, k=call["k"], skip=2, tests=call["tests"], arrivals=call["arrivals"], offset=False
    )
    (i,) = [
        i for i, w in enumerate(r.windows)
        if w.L_edges and round(w.L_edges[0]) == 481 and round(w.L_edges[1]) == 19
    ]  # fmt: skip
    w = r.windows[i]
    assert w.drift["L"]["significant"] is False
    ev = w.drift["backlog"]
    assert ev["kind"] == "backlog_not_steady" and ev["significant"] and abs(ev["z"]) > ev["k"]
    assert "not_steady" in w.flags
    assert i in r.core  # not off the reference (R stays near it): the ratio test alone misses it


def test_the_systematic_offset_names_the_windows_it_rests_on():
    """1i26: `basis` is the windows behind the level (wired as `systematic.windows`): the steady
    non-transient ones. Queueing the timer misses at rho 0.95 (seed 7): windows 7 and 9 are not
    steady and not transient: in `core`, not in the level."""
    r = check(substeps(simulate(7, rates=[(0.0, 9.5)], c=10, timer="service_start")), k=20)
    assert r.systematic is not None and r.systematic.verdict == "L_high"
    ns = {i for i, w in enumerate(r.windows) if "not_steady" in w.flags}
    assert ns == {7, 9} and ns <= set(r.core)
    assert r.basis == [i for i in r.core if i not in ns]
    assert r.systematic.n == sum(r.windows[i].n for i in r.basis)


def test_the_wired_offset_counts_the_windows_behind_the_level():
    """1i26: "in N of M windows" names the level's windows (not every non-transient one)."""
    from telemetry_nerd.core.littles_ops import _systematic_wire

    r = check(substeps(simulate(7, rates=[(0.0, 9.5)], c=10, timer="service_start")), k=20)
    w = _systematic_wire(r)
    assert w is not None and w["windows"] == [len(r.basis), len(r.windows)]
    assert len(r.basis) < len(r.core)
