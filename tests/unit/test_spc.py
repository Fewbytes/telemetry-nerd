"""SPC: detectors, ARL theory, and false-alarm rates on stationary noise vs theory (seeded)."""

import numpy as np
import pytest

from telemetry_nerd.analysis.spc import (
    RULE_RATES,
    control_chart,
    cusum_arl,
    cusum_signals,
    ewma_arl,
    ewma_signals,
    poisson_sf,
    run_rules,
    windows_available,
)
from telemetry_nerd.analysis.stability import fit_harmonics
from tests.unit.test_autocorr import ar1_series


def chart(y, n_base, harmonics=None, t_s=None):
    pos = np.arange(y.size)
    base = pos < n_base
    return control_chart(pos, pos * 60.0 if t_s is None else t_s, y, base, harmonics)


def test_markov_chain_arl_matches_published_tables():
    assert cusum_arl() == pytest.approx(465, rel=0.03)  # k=0.5, h=5: one-sided 930
    assert cusum_arl(mu=1.0) == pytest.approx(10.4, rel=0.05)
    assert ewma_arl() == pytest.approx(558, rel=0.03)  # Lucas & Saccucci, lambda=0.2, L=3
    assert ewma_arl(mu=1.0) == pytest.approx(10.2, rel=0.08)


def test_simulated_arl_matches_markov_chain():
    z = np.random.default_rng(11).normal(size=400_000)
    for signals, arl in ((cusum_signals, cusum_arl()), (ewma_signals, ewma_arl())):
        k = len(signals(z))
        assert z.size / k == pytest.approx(arl, rel=0.12)  # ~800 episodes: +/- 3.5% se


def test_run_rule_rates_match_theory_with_known_parameters():
    rng = np.random.default_rng(3)
    z = rng.normal(size=300_000)
    pos = np.arange(z.size)
    hits = run_rules(z, pos, np.ones(z.size, bool))
    for rule, rate in RULE_RATES.items():
        assert hits[rule].mean() == pytest.approx(rate, rel=0.1), rule


def test_run_rules_break_at_gaps_and_baseline():
    pos = np.array([0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12])  # gap at 4
    z = np.full(pos.size, 0.5)
    hits = run_rules(z, pos, np.ones(pos.size, bool))["8_in_a_row_one_side"]
    assert hits.tolist() == [False] * 11 + [True]  # only 5..12 is a full run of 8
    assert windows_available(pos, np.ones(pos.size, bool), 8) == 1


def test_poisson_tail():
    assert poisson_sf(0, 3) == 1.0
    assert poisson_sf(1, 3) == pytest.approx(1 - np.exp(-3))


@pytest.mark.parametrize("phi", [0.0, 0.7])
def test_false_alarm_rates_on_stationary_noise_match_theory(phi):
    """Limits from a 1000-point baseline judged on 1000 new points, 150 seeds."""
    out = n = 0
    rule_counts = dict.fromkeys(RULE_RATES, 0)
    windows = dict.fromkeys(RULE_RATES, 0)
    episodes = {"ewma": 0, "cusum": 0}
    judged_z = 0
    out_of_control = residual_mode = 0
    for s in range(150):
        c = chart(ar1_series(2000, phi, 100 + s), 1000)
        residual_mode += c.mode == "ar1_residuals"
        out += c.outside.count
        n += c.outside.opportunities
        for r in RULE_RATES:
            rule_counts[r] += c.detectors[r].count
            windows[r] += c.detectors[r].opportunities
        for d in episodes:
            episodes[d] += c.detectors[d].count
        judged_z += c.detectors["ewma"].opportunities
        out_of_control += not c.in_control
    # |phi| > 2/sqrt(n) picks the residual chart for ~5% of white-noise baselines
    assert residual_mode == 150 if phi else residual_mode <= 20
    # drawn band (marginal sigma): nominal 0.27% per point; estimated limits inflate it slightly
    assert 0.0018 < out / n < 0.0045
    for r, rate in RULE_RATES.items():
        assert rule_counts[r] / windows[r] == pytest.approx(rate, rel=0.3), r
    assert judged_z / max(episodes["cusum"], 1) > 0.6 * cusum_arl()
    assert judged_z / max(episodes["ewma"], 1) > 0.6 * ewma_arl()
    assert out_of_control / 150 <= 0.04  # decision level 1% split over three detectors


def test_moving_range_sigma_would_over_alarm_under_autocorrelation():
    """Why the marginal sigma: the conventional MR-bar/1.128 sigma under AR(1) phi=0.7."""
    rates = []
    for s in range(50):
        y = ar1_series(2000, 0.7, s)
        sigma_mr = np.mean(np.abs(np.diff(y[:1000]))) / 1.128
        rates.append(np.mean(np.abs(y[1000:] - np.median(y[:1000])) > 3 * sigma_mr))
        assert chart(y, 1000).sigma > 1.5 * sigma_mr
    assert np.mean(rates) > 5 * RULE_RATES["beyond_3sigma"]


def test_step_change_is_out_of_control_and_located():
    rng = np.random.default_rng(8)
    y = rng.normal(size=600) + np.where(np.arange(600) >= 400, 1.5, 0.0)
    c = chart(y, 300)
    assert not c.in_control
    first = min(c.detectors["cusum"].indices)
    assert 400 <= first < 420
    assert all(i >= 300 for i in c.violations())  # baseline points are never judged
    lo, hi = c.centre_interval
    assert lo < 0 < hi and c.sigma_interval[0] < 1 < c.sigma_interval[1]


def test_small_baseline_is_insufficient_and_stated():
    c = chart(np.random.default_rng(0).normal(size=100), 20)
    assert c.mode == "insufficient_data" and "needs >= 30" in c.reason and c.in_control is None


def test_seasonal_centre_is_fitted_on_baseline_only():
    t = np.arange(0, 4 * 86400, 300.0)
    rng = np.random.default_rng(2)
    y = 10 + 5 * np.sin(2 * np.pi * t / 86400) + rng.normal(size=t.size)
    h = fit_harmonics(t, y, [86400.0])
    c = chart(y, t.size // 2, h, t)
    assert c.seasonal_periods_s == [86400.0] and c.in_control
    assert np.ptp(c.centre) == pytest.approx(10, rel=0.1)
    c_short = chart(y, 200, h, t)  # 200 x 5 min < 2 days: cannot model a daily cycle
    assert "seasonal_not_in_baseline" in c_short.caveats


# gaps (lkn.5) ----------------------------------------------------------------------
def test_ewma_and_cusum_decay_across_gaps_a_long_gap_is_a_restart():
    z = np.r_[np.full(4, 1.2), [1.2]]
    # without gaps the run of 1.2s pushes the CUSUM to 5 x 0.7 = 3.5; one more high point
    # after a long gap must start from scratch, after no gap it continues
    no_gap, long_gap = np.zeros(5, int), np.r_[0, 0, 0, 0, 50]
    assert cusum_signals(np.r_[z, 2.5], gaps=np.r_[no_gap, 0]) == [5]
    assert cusum_signals(np.r_[z, 2.5], gaps=np.r_[long_gap, 0]) == []
    # EWMA: the gap-aware recursion equals the classic one when there are no gaps
    w = np.random.default_rng(1).normal(size=5000)
    assert ewma_signals(w, gaps=np.zeros(w.size, int)) == ewma_signals(w)
    assert cusum_signals(w, gaps=np.zeros(w.size, int)) == cusum_signals(w)
    # a big EWMA excursion is forgotten across a gap of 40 steps ((0.8)^40 ~ 1e-4)
    e = np.full(4, 2.0)
    assert ewma_signals(e) == [3]
    assert ewma_signals(e, gaps=np.r_[0, 0, 0, 40]) == []


def gappy(n, seed, frac=0.1, judged_from=0):
    """Runs of 1..30 missing steps covering ~frac of [judged_from, n)."""
    rng = np.random.default_rng(seed + 10_000)
    keep = np.ones(n, bool)
    while (~keep[judged_from:]).mean() < frac:
        a = int(rng.integers(judged_from, n))
        keep[a : a + int(rng.integers(1, 30))] = False
    return keep


@pytest.mark.parametrize("phi", [0.0, 0.7])
def test_false_alarm_rates_with_gaps_match_theory(phi):
    """10% of the judged points missing in runs: the gap-aware EWMA/CUSUM keep their ARL,
    the run rules (broken at gaps) and the drawn band their rates. 100 seeds."""
    episodes = {"ewma": 0, "cusum": 0}
    judged_z = out = n = out_of_control = 0
    rule_counts = dict.fromkeys(RULE_RATES, 0)
    windows = dict.fromkeys(RULE_RATES, 0)
    for s in range(100):
        keep = gappy(2000, s, judged_from=1000)
        pos = np.flatnonzero(keep)
        c = control_chart(pos, pos * 60.0, ar1_series(2000, phi, 100 + s)[keep], pos < 1000)
        for d in episodes:
            episodes[d] += c.detectors[d].count
        judged_z += c.detectors["ewma"].opportunities
        out += c.outside.count
        n += c.outside.opportunities
        out_of_control += not c.in_control
        for r in RULE_RATES:
            rule_counts[r] += c.detectors[r].count
            windows[r] += c.detectors[r].opportunities
    # measured (seeds 100..199): ARL ewma ~537 / 490 (theory 558), cusum ~469 / 423 (465)
    assert judged_z / max(episodes["ewma"], 1) > 0.75 * ewma_arl()
    assert judged_z / max(episodes["cusum"], 1) > 0.75 * cusum_arl()
    assert 0.0018 < out / n < 0.0045
    for r, rate in RULE_RATES.items():
        assert rule_counts[r] / windows[r] == pytest.approx(rate, rel=0.3), r
    assert out_of_control / 100 <= 0.04


# separate reference baselines and the seasonal residual chart (lkn.5) ----------------
M_MS, HOUR, DAY_MS = 60_000, 3_600_000, 86_400_000


def _daily(ms):
    return 50 + 3 * np.sin(2 * np.pi * (ms % DAY_MS) / DAY_MS)


def _reference_case(seed, phi, shift_ms, use_profile):
    """6 h of 1-min data on a daily cycle (amplitude 3 sigma), judged against a separately
    fetched 6 h window `shift_ms` earlier; the profile shape (if used) comes from 30 days of
    noisy hourly means BEFORE the judged window."""
    from telemetry_nerd.analysis.autocorr import positions
    from telemetry_nerd.analysis.diagnostics import _chart
    from telemetry_nerd.analysis.profile import seasonal_profile, seasonal_shape

    rng = np.random.default_rng(seed)
    start = 40 * DAY_MS + int(rng.integers(0, 24)) * HOUR
    ts = np.arange(start, start + 6 * HOUR, M_MS) + M_MS
    rts = ts - shift_ms
    noise = ar1_series(2 * ts.size, phi, 500 + seed) * np.sqrt(1 - phi * phi)
    y, ry = _daily(ts - M_MS // 2) + noise[ts.size :], _daily(rts - M_MS // 2) + noise[: ts.size]
    prof = None
    if use_profile:
        h = np.arange(start - 30 * DAY_MS, start, HOUR)  # hour starts
        seas = seasonal_profile(h, _daily(h + HOUR // 2) + rng.normal(0, 0.3, h.size))
        prof = (seasonal_shape(seas, np.r_[rts, ts] - M_MS // 2), 86_400.0)
    pos = positions(ts, M_MS)
    return _chart(ts, y, M_MS, pos, (ts - ts[0]) / 1000, np.zeros(ts.size, bool), None, (rts, ry), prof)  # fmt: skip


@pytest.mark.parametrize(("phi", "shift"), [(0.0, 6 * HOUR), (0.7, 6 * HOUR), (0.7, 7 * DAY_MS)])
def test_seasonal_residual_chart_from_the_profile_keeps_the_false_alarm_rate(phi, shift):
    """baseline=previous|week on a daily cycle the 6 h baseline cannot fit (< 2 cycles).
    Measured (150 seeds): a flat centre is unusable (n_eff < 10 for most baselines, the rest
    alarm ~40% of points); the profile's shape gives rule-1 rate 1.25-1.3x nominal (estimated
    limits from 360 points), run rules 1.05-1.2x, EWMA/CUSUM ARL ~450 / ~350, 0% decided out
    of control."""
    flat_bad = out = n = out_of_control = 0
    episodes = {"ewma": 0, "cusum": 0}
    judged_z = 0
    seeds = 60
    for s in range(seeds):
        f = _reference_case(s, phi, shift, False)
        flat_bad += f.mode == "insufficient_data" or not f.in_control or f.outside.count > 5
        c = _reference_case(s, phi, shift, True)
        assert c.seasonal == "profile" and c.mode != "insufficient_data"
        assert c.baseline.size == c.judged.size == 360 and c.judged.all()  # all judged
        out += c.outside.count
        n += c.outside.opportunities
        out_of_control += not c.in_control
        for d in episodes:
            episodes[d] += c.detectors[d].count
        judged_z += c.detectors["ewma"].opportunities
    if shift < DAY_MS:  # the preceding window sits elsewhere on the cycle
        assert flat_bad / seeds > 0.8
    assert 0.0015 < out / n < 0.0055
    assert out_of_control / seeds <= 0.05
    assert judged_z / max(episodes["ewma"], 1) > 0.6 * ewma_arl()
    assert judged_z / max(episodes["cusum"], 1) > 0.5 * cusum_arl()


def test_profile_is_not_used_when_the_baseline_fits_the_cycle_itself():
    t = np.arange(0, 4 * 86400, 300.0)
    rng = np.random.default_rng(2)
    y = 10 + 5 * np.sin(2 * np.pi * t / 86400) + rng.normal(size=t.size)
    h = fit_harmonics(t, y, [86400.0])
    shape = 5 * np.sin(2 * np.pi * t / 86400)
    pos = np.arange(t.size)
    c = control_chart(pos, t, y, pos < t.size // 2, h, shape, 86400.0)
    assert c.seasonal == "harmonics" and c.in_control
    c_short = control_chart(pos, t, y, pos < 200, h, shape, 86400.0)
    assert c_short.seasonal == "profile" and "seasonal_not_in_baseline" not in c_short.caveats
    assert c_short.in_control
