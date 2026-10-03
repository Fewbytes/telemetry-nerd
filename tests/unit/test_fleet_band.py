"""Fleet SPC band, missing-member bounds and both-mode outliers (bead telemetry-nerd-nq6)."""

import math

import numpy as np
import pytest

from telemetry_nerd.analysis.fleet import (
    _pooled,
    analyse,
    check_band_window,
    control_band,
    flagged_steps,
    loo_deviations,
    missing_bounds,
)
from tests.unit.fakes import make_service
from tests.unit.fleet_sim import fleet
from tests.unit.test_fleet_service import put


def test_band_is_the_median_plus_minus_k_of_the_tests_pooled_sigma():
    y, _ = fleet(31, m=100, plant=True)
    f = analyse(y)
    b = control_band(f)
    logy = np.log(y)
    c, s, _ = _pooled(logy)  # the tests' centre and sigma (full fleet: > 64 members)
    assert b.window == 13 and b.pool_half == 6 and b.scale == "log"
    np.testing.assert_allclose(b.centre, np.exp(np.nanmedian(logy, axis=0)))
    np.testing.assert_allclose(b.sigma, s)
    np.testing.assert_allclose(b.hi2, np.exp(c + 2 * s))
    np.testing.assert_allclose(b.lo3, np.exp(c - 3 * s))
    # the per-member z the tests use is d / that sigma (no leave-one-out beyond 64 members)
    d, z, _, loo = loo_deviations(logy)
    assert not loo
    np.testing.assert_allclose(z[0], d[0] / s)
    # the flag line is the spike test's threshold in the same units
    assert b.threshold_z == pytest.approx(f.thresholds["spike_threshold"])
    np.testing.assert_allclose(b.threshold_hi, np.exp(c + b.threshold_z * s))


def test_small_fleet_band_sigma_matches_the_leave_one_out_sigma_closely():
    y, _ = fleet(32, m=30)
    f = analyse(y)
    b = control_band(f)
    d, z, _, loo = loo_deviations(np.log(y))
    assert loo
    s_loo = d / z  # each member's sigma of the others
    ratio = np.nanmedian(s_loo / b.sigma[None, :])
    assert 0.95 < ratio < 1.05


def test_log_band_is_multiplicative_and_linear_band_additive():
    y, _ = fleet(33, m=40)
    b = control_band(analyse(y, scale="log"))
    np.testing.assert_allclose(b.hi2 / b.centre, b.centre / b.lo2, rtol=1e-9)
    np.testing.assert_allclose(b.hi3 / b.centre, (b.hi2 / b.centre) ** 1.5, rtol=1e-9)
    lin = control_band(analyse(y, scale="linear"))
    assert lin.scale == "linear"
    np.testing.assert_allclose(lin.hi2 - lin.centre, lin.centre - lin.lo2, rtol=1e-9)
    np.testing.assert_allclose(lin.hi3 - lin.centre, 1.5 * (lin.hi2 - lin.centre), rtol=1e-9)


def test_band_window_smooths_the_band_and_keeps_the_flag_line():
    y, _ = fleet(34, m=20, phi=0.3)
    f = analyse(y)
    b13, b61 = control_band(f), control_band(f, 61)
    edge = lambda b: np.nanvar(np.diff(np.log(b.hi3)))
    assert edge(b61) < 0.5 * edge(b13)
    assert np.nanvar(np.diff(np.log(b61.centre))) < np.nanvar(np.diff(np.log(b13.centre)))
    if b13.threshold_hi is not None:  # the tests' threshold, whatever the drawn window
        np.testing.assert_allclose(b61.threshold_hi, b13.threshold_hi)
    assert b61.pool_half == 30


@pytest.mark.parametrize("bad", [12, 14, 1, 0])
def test_band_window_must_be_odd_and_at_least_the_tests_pool(bad):
    with pytest.raises(ValueError, match="odd number of steps >= 13"):
        check_band_window(bad)
    assert check_band_window(None) == 13 and check_band_window(15) == 15


def test_missing_member_bounds_on_hand_cases():
    # one step, 5 reporting of 7 alive (m = 2): N = 7, HF7 position h = 6q
    y = np.array([[1.0], [2.0], [3.0], [4.0], [5.0], [np.nan], [np.nan]])
    b = missing_bounds(y, np.array([5]), np.array([7]))
    # median h = 3: lower [-inf, -inf, 1, 2, 3, 4, 5][3] = 2, upper [1..5, inf, inf][3] = 4
    assert b["median_lo"][0] == 2.0 and b["median_hi"][0] == 4.0
    # q25 h = 1.5: lower reaches the missing ranks -> unbounded; upper 2 + 0.5 (3 - 2)
    assert b["q25_lo"][0] == -math.inf and b["q25_hi"][0] == 2.5
    # q75 h = 4.5: lower 3 + 0.5 (4 - 3); upper interpolates into +inf -> unbounded
    assert b["q75_lo"][0] == 3.5 and b["q75_hi"][0] == math.inf
    assert np.isnan(b["q10_lo"][0])  # 10/90 need n >= 10, like the spread
    # nobody missing: the bounds are the quantile itself
    full = missing_bounds(y[:5], np.array([5]), np.array([5]))
    assert full["median_lo"][0] == full["median_hi"][0] == 3.0
    assert full["q25_lo"][0] == full["q25_hi"][0] == 2.0
    # m >= the median's rank: both sides unbounded
    many = missing_bounds(y[:3], np.array([3]), np.array([8]))
    assert many["median_lo"][0] == -math.inf and many["median_hi"][0] == math.inf


def test_missing_member_bounds_bracket_the_full_fleet_quantile():
    y, _ = fleet(35, m=40)
    hidden = y.copy()
    hidden[:4, 100:120] = np.nan  # four members silent
    n = np.sum(~np.isnan(hidden), axis=0)
    b = missing_bounds(hidden, n, np.full(y.shape[1], 40))
    true_med = np.median(y, axis=0)
    sl = slice(100, 120)
    assert np.all(b["median_lo"][sl] <= true_med[sl]) and np.all(true_med[sl] <= b["median_hi"][sl])
    assert np.all(b["median_lo"][:100] == b["median_hi"][:100])


def test_unflagged_beyond_3_sigma_are_counted_and_flagged_steps_excluded():
    y, planted = fleet(36, m=100, plant=True)
    f = analyse(y)
    raw = control_band(f)
    fl = flagged_steps(y.shape, [(o.member, o) for o in f.outliers])
    assert fl[planted["persistent"]].all()
    tran = next(o for o in f.outliers if o.kind == "transient")
    assert fl[tran.member].sum() == sum(e.end - e.start + 1 for e in tran.episodes)
    b = control_band(f, flagged=fl)
    assert 0 < b.outside3.sum() < raw.outside3.sum()
    # about 0.27% of the unflagged member-steps of a normal fleet lie beyond 3 sigma
    assert b.outside3.sum() < 0.01 * y.size


def test_summary_states_the_band_and_the_3_sigma_unflagged_message(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, fleet(37, m=100, plant=True)[0])
    out = svc.fleet(d)
    band = out["band"]
    assert band["basis"] == "median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative)"
    assert band["window_steps"] == 13 and band["source"] == "common_cause"
    assert "not propagated" in band["caveat"] and "not propagated" in out["spread"]["caveat"]
    un = band["outside_3sigma_unflagged"]
    assert un["note"] == "outside 3σ, not significant at fleet-wide 1% (100 members tested)"
    assert un["member_steps"] > 0 and un["members"] > 0
    assert band["flag_threshold_z"] > 3
    wide = svc.fleet(d, band_window=37)["band"]
    assert "pooled ±18 steps" in wide["basis"] and "37-step moving median" in wide["basis"]
    with pytest.raises(ValueError, match="odd"):
        svc.fleet(d, band_window=20)


def test_too_few_tested_members_say_no_tests_ran(tmp_path):
    svc = make_service(tmp_path)
    out = svc.fleet(put(svc, fleet(38, m=6)[0]))
    note = out["band"]["outside_3sigma_unflagged"]["note"]
    assert note == "outside 3σ; no outlier tests ran (6 members tested, 10 needed)"


def test_persistent_member_with_a_spike_carries_episodes_beyond_its_own_level():
    y, planted = fleet(39, m=100, plant=True)
    p = planted["persistent"]
    y[p, 60:64] *= math.exp(1.5)  # a short excursion on top of its x1.8 offset
    f = analyse(y)
    o = next(o for o in f.outliers if o.member == p)
    assert o.kind == "persistent" and o.episodes
    (e,) = o.episodes
    assert e.beyond_own_level and 58 <= e.start <= 61 and 62 <= e.end <= 66
    assert e.peak_z > 0
    # without the spike: a level outlier has no episodes (its offset alone is not an excursion)
    clean = analyse(fleet(39, m=100, plant=True)[0])
    assert not next(o for o in clean.outliers if o.member == p).episodes


def test_panel_carries_spc_band_bounds_and_mode_marks(tmp_path):
    svc = make_service(tmp_path)
    y, planted = fleet(40, m=100, plant=True)
    y[3:6, 200:210] = np.nan  # three members silent: missing-member bounds there
    d = put(svc, y)
    svc.fleet(d, band_window=25)
    shown = svc.show(d, "who is off?", mark="fleet")
    data = svc.panel_data(shown.panel.id, 800)
    spc = data["spc"]
    assert spc["window"] == 25 and spc["pool_half"] == 12
    assert spc["legend"] == "median ± 2σ/3σ (robust, pooled ±12 steps, log scale: multiplicative)"
    for k in ("centre", "lo2", "hi2", "lo3", "hi3", "threshold_lo", "threshold_hi", "outside3"):
        assert len(spc[k]) == 288, k
    assert spc["outside3_note"].startswith("outside 3σ, not significant at fleet-wide 1%")
    assert "not propagated" in spc["note"]
    bb = data["band_bounds"]
    assert bb["median_lo"][205] <= data["band"]["median"][205] <= bb["median_hi"][205]
    assert bb["median_lo"][0] == bb["median_hi"][0] == data["band"]["median"][0]
    by_id = {o["id"]: o for o in data["outliers"]}
    tran = by_id[f"pod=api-{planted['transient']:03d}"]
    (ep,) = tran["episodes"]
    assert set(ep) == {"start_ms", "end_ms", "peak_z", "sustained", "beyond_own_level"}
    assert ep["peak_z"] > 3 and not ep["beyond_own_level"]
    pers = by_id[f"pod=api-{planted['persistent']:03d}"]
    assert pers["effect"]["as"] == "ratio" and 1.5 < pers["effect"]["offset"] < 2.2
    drift = by_id[f"pod=api-{planted['drifting']:03d}"]
    assert drift["effect"]["change_per_hour"] > 1  # a ratio per hour
