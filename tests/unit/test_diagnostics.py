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
    assert run(noise(0, 20)).verdict == "insufficient_data"
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
