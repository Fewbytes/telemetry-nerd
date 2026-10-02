import numpy as np
import pytest

from telemetry_nerd.analysis.autocorr import (
    ar1,
    ar1_residuals,
    lag_pairs,
    n_eff,
    positions,
    tau_int,
)


def ar1_series(n, phi, seed):
    rng = np.random.default_rng(seed)
    e = rng.normal(size=n)
    x = np.empty(n)
    x[0] = e[0] / np.sqrt(1 - phi * phi)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def test_positions_and_pairs_skip_gaps():
    pos = positions(np.array([0, 60_000, 180_000, 240_000]), 60_000)
    assert pos.tolist() == [0, 1, 3, 4]
    a, b = lag_pairs(pos, np.array([1.0, 2.0, 3.0, 4.0]), 1)
    assert a.tolist() == [1.0, 3.0] and b.tolist() == [2.0, 4.0]  # no pair across the gap


@pytest.mark.parametrize("phi", [0.0, 0.5, 0.8])
def test_tau_matches_ar1_theory(phi):
    taus = [tau_int(np.arange(4000), ar1_series(4000, phi, s)) for s in range(10)]
    theory = (1 + phi) / (1 - phi)
    assert np.mean(taus) == pytest.approx(theory, rel=0.15)
    assert n_eff(4000, theory) == pytest.approx(4000 / theory)


def test_tau_with_gaps_uses_only_observed_pairs():
    x = ar1_series(4000, 0.7, 3)
    keep = np.ones(4000, bool)
    for a in range(100, 4000, 400):
        keep[a : a + 50] = False  # 12.5% gaps
    tau = tau_int(np.arange(4000)[keep], x[keep])
    assert tau == pytest.approx(1.7 / 0.3, rel=0.25)


def test_ar1_fit_and_residuals():
    x = ar1_series(5000, 0.6, 1)
    pos = np.arange(5000)
    fit = ar1(pos, x)
    assert fit.phi == pytest.approx(0.6, abs=0.03) and fit.significant
    assert fit.sigma_e == pytest.approx(1.0, rel=0.05)
    ok, e = ar1_residuals(pos, x, fit.phi)
    assert not ok[0] and ok[1:].all() and abs(ar1(pos[ok], e).phi) < 0.05
    white = ar1(pos, np.random.default_rng(2).normal(size=5000))
    assert not white.significant
