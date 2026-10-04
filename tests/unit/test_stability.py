import numpy as np
import pytest

from telemetry_nerd.analysis.stability import (
    changepoints,
    fit_harmonics,
    kpss,
    shape,
    trend,
    variance_ratio,
)
from telemetry_nerd.analysis.stats import kolmogorov_sf, kuiper_sf, t_ppf
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


def test_kuiper_tail():
    assert kuiper_sf(1.747) == pytest.approx(0.05, abs=0.002)
    assert kuiper_sf(2.001) == pytest.approx(0.01, abs=0.001)


@pytest.mark.parametrize("base", [0.0, 10.0])
def test_a_pulse_is_found_at_both_edges(base):
    # a burst that departs and returns (errors from zero during a fault): one split alone
    # misses it, the unmodelled return inflates the two-segment sigma
    pos, ts = grid(64)
    noise = np.random.default_rng(3).normal(scale=0.2, size=64)
    burst = (pos >= 20) & (pos < 40)
    # base 0: exactly zero outside the burst (a counter born on its first event, read as 0)
    y = np.where(burst, base + 2.0 + noise, base + (noise if base else 0.0))
    found = changepoints(pos, ts, y)
    assert [s.index for s in found] == [20, 40] and all(s.p < 1e-6 for s in found)
    assert found[0].delta > 1.5 and found[1].delta < -1.5


def test_short_white_noise_keeps_the_false_alarm_rate():
    # step and pulse alternatives are tested at ALPHA / 2 each (Bonferroni): FAR <= 1% nominal
    pos, ts = grid(64)
    rng = np.random.default_rng(11)
    found = sum(bool(changepoints(pos, ts, rng.normal(size=64))) for _ in range(400))
    assert found <= 9  # 4 expected at 1%; P(> 9) < 1%


def _far(n, phi, runs, seed):
    pos, ts = grid(n)
    return sum(
        bool(changepoints(pos, ts, ar1_series(n, phi, seed * 10_000 + i))) for i in range(runs)
    )


@pytest.mark.parametrize("n", [64, 128])
@pytest.mark.parametrize("phi", [0.0, 0.6])
def test_short_autocorrelated_noise_keeps_the_false_alarm_rate(n, phi):
    # nbz: AR(1) phi fitted to short, separately demeaned split residuals is biased low (Kendall
    # -(1 + 3 phi) / n per segment, more for a selected boundary), so the long-run sigma was too
    # small: 21 of 400 at n 64, phi 0.6 (5%; nominal 1%). Bound 9 of 400: P(> 9 | 1%) = 0.8%
    # (Binomial(400, p)); a true FAR of 5% passes with P = 0.4%. Now 1-5 of 400.
    assert _far(n, phi, 400, seed=n) <= 9


@pytest.mark.parametrize("n", [64, 128])
def test_near_unit_root_short_noise_false_alarm_rate_is_bounded(n):
    # phi 0.9 at n 64 is ~3 effective samples: the cautious phi still runs low often enough for
    # a simulated FAR of ~2% (1000 runs; SHIFT_METHOD states it); was 25%. Bound 16 of 400:
    # P(> 16 | 2%) = 0.3%, a true 8% passes with P = 0.1%. Was 107 / 62 of 400, now 2 / 8.
    assert _far(n, 0.9, 400, seed=n + 1) <= 16


def test_a_shift_only_the_point_model_sees_is_kept_apart():
    # AR(1) phi 0.6 plus a 2.5-unit step at 32 of 64: significant under the bias-corrected point
    # phi, not under the cautious one (phi + 1 SE): no labelled shift, the point model finds it
    pos, ts = grid(64)
    y = ar1_series(64, 0.6, 1) + np.where(pos >= 32, 2.5, 0.0)
    assert changepoints(pos, ts, y) == []
    (sh,) = changepoints(pos, ts, y, cautious=False)
    assert sh.index == 32 and sh.p_point < 0.01 <= sh.p


def test_point_p_is_never_above_the_cautious_p():
    pos, ts = grid(128)
    for s in range(20):
        y = ar1_series(128, 0.7, s) + np.where(pos >= 50, 3.0, 0.0)
        assert all(sh.p_point <= sh.p for sh in changepoints(pos, ts, y, cautious=False))


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
