"""Autocorrelation of gappy step series (bead lkn.1). Pure numpy, no I/O.

Samples sit on a step grid; `pos` is each sample's integer step index. A lag-k pair exists only
when both points were observed: gaps are never interpolated, they just contribute no pairs.
τ_int (integrated autocorrelation time) by Geyer's initial positive sequence; the effective
sample size of a mean is n / τ_int. Reused by seasonal comparison (lkn.2) and fleets (lkn.3).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

MAX_LAG_SHARE = 4  # lags up to n / 4: beyond that the pairs are too few to estimate rho


def positions(ts_ms: np.ndarray, step_ms: int) -> np.ndarray:
    """Integer step index of each sample, 0 at the first one."""
    return ((np.asarray(ts_ms, np.int64) - int(ts_ms[0])) // step_ms).astype(np.int64)


def _dense(pos: np.ndarray, x: np.ndarray) -> np.ndarray:
    out = np.full(int(pos[-1]) + 1, np.nan)
    out[pos] = x
    return out


def lag_pairs(pos: np.ndarray, x: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray]:
    """(x_t, x_{t+lag}) for every t where both were observed."""
    d = _dense(pos, x)
    a, b = d[:-lag], d[lag:]
    ok = ~np.isnan(a) & ~np.isnan(b)
    return a[ok], b[ok]


def tau_int(pos: np.ndarray, x: np.ndarray) -> float:
    """Integrated autocorrelation time, Geyer (1992) initial positive (monotone) sequence.

    τ = −1 + 2 Σ Γ_m with Γ_m = ρ_2m + ρ_2m+1, summed while positive and non-increasing.
    1 for white noise, (1 + φ)/(1 − φ) for AR(1). Never below 1 (conservative)."""
    n = x.size
    max_lag = max(2, n // MAX_LAG_SHARE)
    d = _dense(pos, np.asarray(x, float) - float(np.mean(x)))
    c0 = float(np.mean(np.square(x - np.mean(x))))
    if c0 == 0:
        return 1.0

    def rho(k: int) -> float:
        if k == 0:
            return 1.0
        if k >= d.size:
            return math.nan
        p = d[:-k] * d[k:]
        ok = ~np.isnan(p)
        return float(p[ok].mean()) / c0 if ok.sum() >= 3 else math.nan

    total, prev = 0.0, math.inf
    for m in range(max_lag // 2 + 1):
        r0, r1 = rho(2 * m), rho(2 * m + 1)
        if math.isnan(r0) or math.isnan(r1):
            break
        g = min(r0 + r1, prev)
        if g <= 0:
            break
        total += g
        prev = g
    return max(1.0, -1.0 + 2.0 * total)


def n_eff(n: int, tau: float) -> float:
    return float(n) / max(1.0, tau)


@dataclass(frozen=True)
class AR1:
    phi: float
    se: float
    n_pairs: int
    sigma_e: float  # standard deviation of the one-step residuals

    @property
    def significant(self) -> bool:
        """|phi| > 2/sqrt(n): the conventional ~95% white-noise bound on a lag-1 coefficient."""
        return self.n_pairs >= 3 and abs(self.phi) > 2 / math.sqrt(self.n_pairs)


def ar1(pos: np.ndarray, d: np.ndarray) -> AR1:
    """AR(1) through the origin on deviations d (already centred), from adjacent pairs only."""
    a, b = lag_pairs(pos, np.asarray(d, float), 1)
    if a.size < 3 or float(a @ a) == 0:
        return AR1(0.0, math.inf, int(a.size), float(np.std(d)))
    phi = float(np.clip((a @ b) / (a @ a), -0.99, 0.99))
    e = b - phi * a
    return AR1(phi, math.sqrt(max(1e-12, 1 - phi * phi) / a.size), int(a.size), float(e.std()))


def ar1_residuals(pos: np.ndarray, d: np.ndarray, phi: float) -> tuple[np.ndarray, np.ndarray]:
    """e_t = d_t − φ d_{t−1} where t−1 was observed. Returns (mask over samples, residuals)."""
    dense = _dense(pos, np.asarray(d, float))
    prev = np.r_[np.nan, dense[:-1]][pos]
    ok = ~np.isnan(prev)
    return ok, (d - phi * np.nan_to_num(prev))[ok]


def dispersion(pos: np.ndarray, events: np.ndarray) -> float | None:
    """Long-run variance-to-mean ratio of event counts per step: quasi-Poisson phi (sample
    variance / mean) x the integrated autocorrelation time, so a sum over k steps has variance
    ~ D x its mean. None with fewer than 2 points or no events. (analyze's departure test, the
    Little's law cautious envelope.)"""
    x = np.asarray(events, float)
    m = float(x.mean()) if x.size else 0.0
    if x.size < 2 or m <= 0:
        return None
    return float(x.var(ddof=1)) / m * tau_int(np.asarray(pos), x)


def widest(candidates: Mapping[str, float]) -> tuple[str, float]:
    """The largest candidate dispersion (a cautious model's) and its source; a tie goes to the
    earlier candidate, so list the more modest source first."""
    source = max(candidates, key=candidates.__getitem__)
    return source, candidates[source]
