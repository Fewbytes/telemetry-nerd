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
