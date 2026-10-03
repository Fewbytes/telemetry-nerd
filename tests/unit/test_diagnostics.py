"""Acceptance: synthetic series with known structure get the right verdict (seeded)."""

import numpy as np
import pytest

from telemetry_nerd.analysis.diagnostics import diagnose
from telemetry_nerd.analysis.spectrum import spectrum
from tests.unit.test_autocorr import ar1_series

M = 60_000
N = 1440  # one day at 1m


def run(y, step=M, base_share=0.5, gaps=None):
    ts = np.arange(y.size, dtype=np.int64) * step
    if gaps is not None:
        ts, y = ts[gaps], y[gaps]
    base = ts < ts[0] + base_share * (ts[-1] - ts[0] + step)
    try:
        sp = spectrum(ts, y, step, top=8)
    except ValueError:
        sp = None
    return diagnose(ts, y, step, base, sp)


def noise(seed, n=N):
    return np.random.default_rng(seed).normal(size=n)


@pytest.mark.parametrize("seed", range(5))
def test_white_noise_is_stable(seed):
    d = run(noise(seed))
    assert d.verdict == "stable", d.reasons
    assert d.chart.in_control and d.chart.mode in ("individuals", "ar1_residuals")


@pytest.mark.parametrize("seed", range(5))
def test_ar1_noise_is_not_called_a_drift_or_shift(seed):
    d = run(ar1_series(N, 0.7, seed))
    assert d.verdict in ("stable", "noisy"), d.reasons
    assert "drifting" not in d.also and "level_shifted" not in d.also
    assert d.chart.mode == "ar1_residuals" and d.tau > 3


@pytest.mark.parametrize("seed", range(5))
def test_known_period(seed):
    t = np.arange(N)
    d = run(3 * np.sin(2 * np.pi * t / 60) + noise(seed))
    assert d.verdict == "periodic", d.reasons
    assert abs(d.peaks[0].period_ms - 3_600_000) < 60_000
    assert d.chart.seasonal_periods_s == [pytest.approx(3600, rel=0.02)] and d.chart.in_control


@pytest.mark.parametrize("seed", range(5))
def test_step_change(seed):
    t = np.arange(N)
    d = run(noise(seed) + np.where(t >= 1000, 2.0, 0.0))
    assert d.verdict == "level_shifted", d.reasons
    (s,) = d.shifts
    assert abs(s.index - 1000) <= 5 and s.interval[0] < 2 < s.interval[1]
    assert d.chart.in_control is False


@pytest.mark.parametrize("seed", range(5))
def test_drift(seed):
    t = np.arange(N)
    d = run(noise(seed) + 3 * t / N)
    assert d.verdict == "drifting", d.reasons
    assert d.trend.change_interval[0] < 3 < d.trend.change_interval[1]


def test_period_and_step_together():
    t = np.arange(N)
    d = run(3 * np.sin(2 * np.pi * t / 60) + noise(1) + np.where(t >= 1000, 3.0, 0.0))
    assert d.verdict == "level_shifted" and "periodic" in d.also, d.reasons


def test_insufficient_data():
    d = run(noise(0, 15))
    assert d.verdict == "insufficient_data" and d.reasons == ["15 points < 16"]
    d = run(ar1_series(300, 0.995, 1))
    assert d.verdict == "insufficient_data" and "n_eff" in d.reasons[0]


def test_gaps_are_not_interpolated():
    keep = np.ones(N, bool)
    keep[300:400] = keep[900:950] = False
    t = np.arange(N)
    d = run(noise(2) + np.where(t >= 1000, 2.0, 0.0), gaps=keep)
    assert d.verdict == "level_shifted" and d.n == keep.sum()
    assert d.shifts[0].ts_ms == pytest.approx(1000 * M, abs=5 * M)


def test_heteroscedastic_noise_is_noisy():
    t = np.arange(N)
    d = run(noise(3) * np.where(t >= N // 2, 3.0, 1.0))
    assert d.verdict == "noisy", d.reasons
    assert any("variance" in r for r in d.reasons)


def test_a_short_series_is_judged_without_a_period_search():
    """< 32 points (e.g. 30 min at 1m = 31): shifts, trend and SPC are judged, periods are not
    searched (the spectrum's minimum), stated as a caveat (7thi)."""
    t = np.arange(31)
    d = run(noise(3, 31) + np.where(t >= 16, 6.0, 0.0))
    assert d.verdict == "level_shifted" and "no_period_search" in d.caveats, d.reasons
    assert not d.peaks and d.shifts[0].ts_ms == 16 * M


# --- departure from an all-zero baseline (event counts) ---------------------------------------

BURST = np.r_[np.zeros(14), [9.0, 14, 12, 13, 13], np.zeros(3)]  # round 4 payment: 61 errors


def zero_base(y, n_base=13, scale=1.0):
    ts = np.arange(y.size, dtype=np.int64) * M
    return diagnose(ts, y, M, np.arange(y.size) < n_base, None, events_scale=scale)


def test_a_burst_after_a_zero_baseline_departs_from_zero():
    d = zero_base(BURST)
    dep = d.departure
    assert dep is not None and dep.significant and dep.events == 61
    assert (dep.n_baseline, dep.n_judged, dep.index) == (13, 9, 14)
    assert dep.p == pytest.approx((9 / 22) ** 61)
    assert dep.mean == pytest.approx(61 / 9) and dep.interval[0] < dep.mean < dep.interval[1]
    # too short for a changepoint segment: the departure carries the special cause
    assert not d.shifts and d.verdict == "level_shifted", d.reasons
    assert any(v["source"] == "special_cause" and "departure" in v["finding"]
               for v in d.variation)  # fmt: skip


def test_departure_needs_counts_a_zero_baseline_and_enough_events():
    assert zero_base(BURST, scale=None).departure is None  # not an event count: no test
    nonzero = BURST.copy()
    nonzero[3] = 1.0
    assert zero_base(nonzero).departure is None  # the baseline saw an event: SPC's job
    assert zero_base(BURST, n_base=7).departure is None  # < MIN_SEGMENT baseline steps
    one = np.r_[np.zeros(14), [1.0], np.zeros(7)]
    d = zero_base(one)  # 1 event: (9/22)^1, one rate explains it
    assert d.departure is not None and not d.departure.significant
    assert d.verdict != "level_shifted"
    assert any(v["source"] == "common_cause" and "zero baseline" in v["finding"]
               for v in d.variation)  # fmt: skip
    # rate(x[1m]) per second at 1m: x 60 events per step; 0.1/s for 5 min = 30 events
    rate = np.r_[np.zeros(14), np.full(5, 0.1), np.zeros(3)]
    assert zero_base(rate, scale=60.0).departure.events == 30
