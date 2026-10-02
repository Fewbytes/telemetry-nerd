"""Distributions and robust scale without scipy, shared by series diagnostics, SPC, seasonal
comparison and fleets (epic lkn). Pure.

Student t two ways: `t_ppf` / `t_quantile` by the Cornish-Fisher expansion (Abramowitz & Stegun
26.7.5; cheap, ~1e-3 for df >= 3), `t_sf` / `t_isf` from the regularized incomplete beta (exact
in the far tail, fractional df). The Kolmogorov distribution as its alternating series; Poisson
and binomial tails by summation.
"""

from __future__ import annotations

import math
from functools import lru_cache
from statistics import NormalDist

import numpy as np

MAD_SCALE = 1.4826  # MAD -> sigma for a normal
MAD_VAR = 1.3605  # n * var(sigma_MAD) / sigma^2 for a normal (MAD's 37% efficiency)


def robust_sigma(x: np.ndarray) -> float:
    return MAD_SCALE * float(np.median(np.abs(x - np.median(x))))


def z_of(p: float) -> float:
    return NormalDist().inv_cdf(p)


# Student t: Cornish-Fisher ---------------------------------------------------------------------
def t_ppf(p: float, df: float) -> float:
    """Student-t quantile; Cornish-Fisher in 1/df (A&S 26.7.5), within ~1e-3 for df >= 3."""
    z = z_of(p)
    if df > 1e6:
        return z
    df = max(df, 1.0)
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    g4 = (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z) / 92160
    return z + g1 / df + g2 / df**2 + g3 / df**3 + g4 / df**4


def t_quantile(p: float, df: float) -> float:
    """Student t quantile: exact for df 1 and 2, Cornish-Fisher beyond (t_ppf)."""
    if df <= 1:
        return math.tan(math.pi * (p - 0.5))
    if df <= 2:
        return (2 * p - 1) / math.sqrt(2 * p * (1 - p))
    return t_ppf(p, df)


# Student t: incomplete beta --------------------------------------------------------------------
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x)
    lbt += b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def t_sf(x: float, df: float) -> float:
    """P(T > x) for Student t with df degrees of freedom (df may be fractional)."""
    if x == 0:
        return 0.5
    tail = 0.5 * _betainc(df / 2, 0.5, df / (df + x * x))
    return tail if x > 0 else 1 - tail


@lru_cache(maxsize=4096)
def t_isf(p: float, df: float) -> float:
    """x with P(T > x) = p (0 < p < 0.5), by bisection on t_sf: accurate in the far tail."""
    lo, hi = 0.0, 1.0
    while t_sf(hi, df) > p:
        hi *= 2
        if hi > 1e12:
            return math.inf
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_sf(mid, df) > p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10 * hi:
            break
    return hi


# tails -----------------------------------------------------------------------------------------
def kolmogorov_sf(x: float) -> float:
    """P(sup |Brownian bridge| > x) = 2 Σ (−1)^(k−1) exp(−2 k² x²)."""
    if x <= 0.2:
        return 1.0
    k = np.arange(1, 101)
    return float(np.clip(2 * np.sum((-1.0) ** (k - 1) * np.exp(-2 * k * k * x * x)), 0.0, 1.0))


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k), X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    term = total = math.exp(-lam)
    for i in range(1, k):
        term *= lam / i
        total += term
    return max(0.0, 1.0 - total)


def binom_sf(x: int, n: int, p: float) -> float:
    """P(Bin(n, p) >= x)."""
    if x <= 0:
        return 1.0
    if x > n:
        return 0.0
    return float(
        min(1.0, sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(x, n + 1)))
    )
