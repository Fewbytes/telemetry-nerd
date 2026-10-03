"""Fleet SPC band, missing-member bounds and both-mode outliers (bead telemetry-nerd-nq6)."""

import math
import re

import numpy as np
import pytest

from telemetry_nerd.analysis.fleet import (
    P_BEYOND_3,
    _pooled,
    analyse,
    check_band_window,
    control_band,
    flagged_steps,
    loo_deviations,
    max_band_window,
    missing_bounds,
    widening,
    widening_count,
)
from telemetry_nerd.analysis.stats import binom_sf
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
    assert 0 < b.outside3_total < raw.outside3_total
    assert b.cells == raw.cells - int((fl & np.isfinite(y)).sum())
    # the count against what a normal fleet gives: 0.27% of the unflagged member-steps
    assert b.expected3 == pytest.approx(0.0026998 * b.cells, rel=1e-3)
    assert 0.3 * b.expected3 < b.outside3_total < 3 * b.expected3


def test_widening_is_a_family_wise_overdispersed_p_chart():
    assert widening_count(100) == 3  # P(Bin(100, 0.27%) >= 3) < 1% <= P(>= 2)
    assert binom_sf(3, 100, P_BEYOND_3) < 0.01 <= binom_sf(2, 100, P_BEYOND_3)
    # p_hat is the window's own share, never below 0.27%; alpha is split over the steps
    count, n_t = np.zeros(200, int), np.full(200, 100)
    marks, p_hat, phi, alpha = widening(count, n_t)
    assert not marks.any() and p_hat == P_BEYOND_3 and phi == 1.0 and alpha == 0.01 / 200
    count[::10] = 3  # 3 beyond 3 sigma every 10th step: the fleet's own shape, p_hat = 0.3%
    marks, p_hat, phi, _ = widening(count, n_t)
    assert p_hat == pytest.approx(0.003) and phi > 1 and not marks.any()
    count[150] = 25  # one step far wider than the fleet's own share: marked
    marks, *_ = widening(count, n_t)
    assert marks[150] and marks.sum() == 1
    # planted: one step where every member's spread jumps
    y, _ = fleet(41, m=100)
    wide = y.copy()
    wide[:, 150] = np.exp(np.log(wide[:, 150]) + np.random.default_rng(0).normal(0, 0.6, 100))
    wb = control_band(analyse(wide))
    assert wb.widening[150] and wb.widening.sum() <= 3


@pytest.mark.parametrize("df", [None, 4.0])
def test_widening_null_any_mark_in_long_windows_is_rare(df):
    """100 members x 1440 steps, AR(0.6) normal and t(4) noise: the chance of ANY widening mark
    in the window is designed at 1% (offline, 150 seeds each: 0 marked fleets)."""
    fleets_marked = 0
    for seed in range(8):
        y, _ = fleet(5000 + seed, m=100, t=1440, df=df)
        f = analyse(y)
        b = control_band(f, flagged=flagged_steps(y.shape, [(o.member, o) for o in f.outliers]))
        fleets_marked += bool(b.widening.any())
    assert fleets_marked <= 1


def test_unknown_member_steps_are_neither_counted_nor_in_the_band():
    y, _ = fleet(42, m=40)
    unknown = np.zeros(y.shape, bool)
    unknown[:, 100:130] = True
    y_bad = y.copy()
    y_bad[:, 100:130] *= 50  # garbage that came back from a failed chunk
    f = analyse(y_bad, unknown=unknown)
    b = control_band(f)
    assert np.all(np.isnan(b.centre[100:130])) and b.outside3[100:130].sum() == 0
    clean = control_band(analyse(y))
    assert b.cells == clean.cells - 40 * 30


def test_band_window_is_capped_and_pooling_is_memory_bounded(monkeypatch):
    from telemetry_nerd.analysis import fleet as fl

    assert max_band_window(None) == 121 and max_band_window(100) == 99 and max_band_window(8) == 7
    with pytest.raises(ValueError, match="<= 121"):
        check_band_window(123)
    with pytest.raises(ValueError, match="<= 49 .*50 steps"):
        check_band_window(51, steps=50)
    assert check_band_window(13, steps=8) == 13  # the tests' own pool always draws
    y, _ = fleet(43, m=30, t=200)
    want = _pooled(np.log(y), 30)
    monkeypatch.setattr(fl, "POOL_CELLS", 500)  # tiny blocks: same result
    got = fl._pooled(np.log(y), 30)
    for a, b in zip(want, got, strict=True):
        np.testing.assert_allclose(a, b)


def test_summary_states_the_band_and_the_3_sigma_count(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, fleet(37, m=100, plant=True)[0])
    out = svc.fleet(d)
    band = out["band"]
    assert band["basis"] == "median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative)"
    assert band["window_steps"] == 13 and band["source"] == "common_cause"
    assert "not propagated" in band["caveat"] and "not propagated" in out["spread"]["caveat"]
    un = band["outside_3sigma_unflagged"]
    assert re.fullmatch(
        r"\d+ member-steps beyond 3σ unflagged \([\d.]+%; 0\.27% if normal\)", un["note"]
    )
    assert un["rate_if_normal"] == pytest.approx(0.0027, rel=1e-2)
    assert un["each"] == "outside 3σ, not significant at fleet-wide 1% (100 members tested)"
    assert un["member_steps"] > 0 and un["expected_if_normal"] > 0
    assert band["flag_threshold_z"] > 3 and "approximate" in band["flag_line"]
    assert "faster than the ±6-step σ tracks" in band["widening"]["note"]
    assert "chance of any mark in this window ≈ 1%" in band["widening"]["rule"]
    assert band["widening"]["source"] == "common_cause"
    wide = svc.fleet(d, band_window=37)["band"]
    assert "pooled ±18 steps" in wide["basis"] and "37-step moving median" in wide["basis"]
    assert "lags" in wide["smoothing"]
    with pytest.raises(ValueError, match="odd"):
        svc.fleet(d, band_window=20)


def test_too_few_tested_members_say_no_tests_ran(tmp_path):
    svc = make_service(tmp_path)
    out = svc.fleet(put(svc, fleet(38, m=6)[0]))
    note = out["band"]["outside_3sigma_unflagged"]["each"]
    assert note == "outside 3σ; no outlier tests ran (6 members tested, 10 needed)"


def test_persistent_member_with_a_spike_carries_episodes_beyond_its_own_level(tmp_path):
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
    # summary: uncalibrated (bead db0), a lead, not a finding
    svc = make_service(tmp_path)
    item = next(o for o in svc.fleet(put(svc, y))["outliers"] if o["member"] == f"pod=api-{p:03d}")
    assert item["episodes"][0]["calibrated"] is False and "not calibrated" in item["episodes_note"]


def test_both_mode_episodes_on_clean_level_and_change_members_are_rare():
    """Null check for the uncalibrated both-mode scan (bead db0): clean persistent and drifting
    members (no excursion planted) across seeds; the share that gets an episode beyond its own
    level. Normal AR(1) noise, 15 seeds x 8 members: 0 of 120 (100 seeds: 0 of 800; with t(4)
    noise 21 of 800, 2.6%: recorded in db0)."""
    flagged = with_ep = 0
    for seed in range(15):
        y, _ = fleet(1000 + seed, m=60)
        t_ = y.shape[1]
        for i in range(8):
            y[i] *= np.exp(0.5 + 0.1 * i) if i < 4 else np.exp(0.9 * np.arange(t_) / (t_ - 1))
        for o in analyse(y).outliers:
            if o.member < 8 and o.kind != "transient":
                flagged += 1
                with_ep += bool(o.episodes)
    assert flagged >= 100
    assert with_ep / flagged <= 0.02


def test_panel_carries_spc_band_sparse_bounds_and_mode_marks(tmp_path):
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
    for k in ("centre", "lo2", "hi2", "lo3", "hi3", "threshold_lo", "threshold_hi"):
        assert len(spc[k]) == 288, k
    assert "outside3" not in spc and spc["outside3_total"] >= 0 and spc["outside3_expected"] > 0
    assert spc["outside3_note"].startswith("outside 3σ, not significant at fleet-wide 1%")
    assert "approximate" in spc["threshold_note"] and "leave-one-out" in spc["threshold_note"]
    assert all(0 <= j < 288 for j in spc["widening"])
    assert "not propagated" in data["member_error_note"]
    bb = data["band_bounds"]
    assert bb["steps"] == list(range(200, 210)) and bb["missing"] == [3] * 10
    k = bb["steps"].index(205)
    assert bb["median_lo"][k] <= data["band"]["median"][205] <= bb["median_hi"][k]
    by_id = {o["id"]: o for o in data["outliers"]}
    tran = by_id[f"pod=api-{planted['transient']:03d}"]
    (ep,) = tran["episodes"]
    assert set(ep) == {"start_ms", "end_ms", "peak_z", "sustained", "beyond_own_level"}
    assert ep["peak_z"] > 3 and not ep["beyond_own_level"]
    pers = by_id[f"pod=api-{planted['persistent']:03d}"]
    assert pers["since_window_start"] and pers["effect"]["over"] == "window"
    assert pers["effect"]["as"] == "ratio" and 1.5 < pers["effect"]["offset"] < 2.2
    drift = by_id[f"pod=api-{planted['drifting']:03d}"]
    assert drift["effect"]["change_per_hour"] > 1  # a ratio per hour
    # nobody missing: no bounds on the wire
    full = svc.panel_data(svc.show(put(svc, fleet(40, m=100)[0]), "q", mark="fleet").panel.id, 800)
    assert "band_bounds" not in full


def test_grouped_panel_and_summary_carry_each_groups_own_band(tmp_path):
    svc = make_service(tmp_path)
    y, _ = fleet(18, m=100, plant=True)
    y[:30] *= 2.0
    labels = [{"pod": f"api-{i:03d}", "size": "small" if i < 30 else "large"} for i in range(100)]
    d = put(svc, y, labels=labels)
    out = svc.fleet(d, by=["pod"])
    assert out["band"]["basis"].startswith("per behaviour group")
    groups = out["clusters"]["groups"]
    for g in groups:
        assert g["band"]["flag_threshold_z"] > 3 and "outside_3sigma_unflagged" in g["band"]
    total = sum(g["band"]["outside_3sigma_unflagged"]["member_steps"] for g in groups)
    assert out["band"]["outside_3sigma_unflagged"]["member_steps"] == total
    data = svc.panel_data(svc.show(d, "who is off?", mark="fleet").panel.id, 800)
    assert "spc" not in data  # the whole-fleet band would sit between the groups
    for c in data["clusters"]:
        assert len(c["spc"]["centre"]) == 288 and c["spc"]["threshold_hi"] is not None
        assert c["spc"]["legend"].startswith("median ± 2σ/3σ")
    small, large = sorted(data["clusters"], key=lambda c: c["size"])
    assert np.nanmedian(np.array(small["spc"]["centre"], float)) > 1.5 * np.nanmedian(
        np.array(large["spc"]["centre"], float)
    )


def test_a_member_marked_stale_is_gone_not_missing_in_the_bounds():
    y = np.array([[1.0], [2.0], [3.0], [4.0], [5.0], [np.nan], [np.nan]])
    b = missing_bounds(y, np.array([5]), np.array([7]), gone=np.array([1]))  # m = 1
    # N = 6, median h = 2.5: lower [-inf, 1, 2, 3, 4, 5] -> 2.5, upper [1..5, inf] -> 3.5
    assert b["median_lo"][0] == 2.5 and b["median_hi"][0] == 3.5


def test_panel_bounds_drop_members_the_source_marked_stale(tmp_path, monkeypatch):
    """I5: a member the source marked stale (churn `ended`) has no value to bound after its
    stale point; trailing silence without a marker still counts as missing (alive, silent)."""
    import dataclasses

    import polars as pl

    from telemetry_nerd.core import fleet_ops
    from telemetry_nerd.model.bucket_state import STATE_SCHEMA, Flag
    from telemetry_nerd.model.series import series_id
    from tests.unit.test_fleet_service import STEP, T0

    y, _ = fleet(21, m=30)
    y[4, 200:] = np.nan
    svc = make_service(tmp_path)
    silent = svc.panel_data(svc.show(put(svc, y), "q", mark="fleet").panel.id, 800)
    assert silent["band_bounds"]["steps"][0] == 200 and set(silent["band_bounds"]["missing"]) == {1}
    real = fleet_ops.dataset_bundle

    def marked(store, meta, result):
        b = real(store, meta, result)
        st = pl.from_arrow(b.companions["bucket_state"])
        sid = series_id("default", {"pod": "api-004", "job": "api"})
        st = st.with_columns(
            pl.when((pl.col("series_id") == sid) & (pl.col("ts_ms") == T0 + 200 * STEP))
            .then(int(Flag.STALE_MARKER)).otherwise(pl.col("flags")).cast(pl.UInt16).alias("flags")
        )  # fmt: skip
        return dataclasses.replace(b, companions={"bucket_state": st.to_arrow().cast(STATE_SCHEMA)})

    monkeypatch.setattr(fleet_ops, "dataset_bundle", marked)
    (tmp_path / "b").mkdir()
    svc2 = make_service(tmp_path / "b")
    ended = svc2.panel_data(svc2.show(put(svc2, y), "q", mark="fleet").panel.id, 800)
    assert "band_bounds" not in ended
