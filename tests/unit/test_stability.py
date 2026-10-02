import numpy as np
import pytest

from telemetry_nerd.analysis.stability import (
    changepoints,
    fit_harmonics,
    kolmogorov_sf,
    kpss,
    shape,
    t_ppf,
    trend,
    variance_ratio,
)
from tests.unit.test_autocorr import ar1_series

M = 60_000


def grid(n):
    return np.arange(n), np.arange(n, dtype=np.int64) * M


@pytest.mark.parametrize(("df", "q"), [(5, 2.571), (10, 2.228), (30, 2.042), (1e9, 1.960)])
def test_t_quantiles(df, q):
    assert t_ppf(0.975, df) == pytest.approx(q, abs=0.01)


def test_kolmogorov_tail():
    assert kolmogorov_sf(1.358) == pytest.approx(0.05, abs=0.002)
    assert kolmogorov_sf(1.628) == pytest.approx(0.01, abs=0.001)


def test_trend_interval_covers_truth_under_ar1():
    pos, ts = grid(600)
    hits = 0
    for s in range(40):
        y = 0.01 * pos + ar1_series(600, 0.7, s)  # 0.6 per hour
        tr = trend(pos, ts, y, 600 * M)
        hits += tr.interval_per_h[0] <= 0.6 <= tr.interval_per_h[1]
    assert hits >= 36  # 99% nominal; Cornish-Fisher + tau estimate: allow some slack


def test_trend_on_ar1_noise_is_rarely_significant():
    pos, ts = grid(600)
    sig = sum(trend(pos, ts, ar1_series(600, 0.8, s), 600 * M).significant for s in range(60))
    assert sig <= 5  # nominal 1%; a naive OLS SE would flag most of them


def test_changepoint_found_at_the_step_with_interval():
    pos, ts = grid(400)
    y = np.random.default_rng(1).normal(size=400) + np.where(pos >= 250, 2.0, 0.0)
    (sh,) = changepoints(pos, ts, y)
    assert abs(sh.index - 250) <= 3 and sh.p < 1e-6
    assert sh.interval[0] <= 2.0 <= sh.interval[1] and sh.n_before + sh.n_after == 400


def test_no_changepoints_in_noise():
    pos, ts = grid(400)
    found = sum(bool(changepoints(pos, ts, ar1_series(400, 0.6, s))) for s in range(50))
    assert found <= 4


def test_kpss_white_vs_random_walk():
    rng = np.random.default_rng(0)
    assert not kpss(rng.normal(size=500)).reject_5pct
    walk = kpss(np.cumsum(rng.normal(size=500)))
    assert walk.reject_5pct and walk.p_upper == 0.01


def test_variance_ratio_detects_doubling():
    rng = np.random.default_rng(4)
    pos, _ = grid(600)
    y = rng.normal(size=600) * np.where(pos >= 300, 2.0, 1.0)
    vr = variance_ratio(pos, y)
    assert vr.significant and vr.interval[0] <= 2 <= vr.interval[1]
    assert not variance_ratio(pos, rng.normal(size=600)).significant


def test_shape_flags():
    rng = np.random.default_rng(5)
    exp = shape(rng.exponential(size=800), 1.0)
    assert exp.flags == ["skewed", "heavy_tails"] and exp.skew_interval[0] > 1
    zi = shape(np.where(rng.random(800) < 0.3, 0.0, rng.normal(5, 1, 800)), 1.0)
    assert {"zero_inflated", "bimodal"} <= set(zi.flags) and zi.zeros > 200
    lo, hi = zi.zero_share_interval
    assert lo < zi.zeros / 800 < hi
    assert shape(rng.normal(size=800), 1.0).flags == []


def test_harmonics_recover_amplitude():
    t = np.arange(0, 4 * 86400, 60.0)
    y = 3 * np.sin(2 * np.pi * t / 86400 + 0.3) + 1
    h = fit_harmonics(t, y, [86400.0])
    assert h.amplitudes()[0] == pytest.approx(3, rel=1e-6)
    assert np.allclose(y - h.curve(t), 1)
