"""Is a series stable? Trend, level shifts, stationarity, variance, shape (bead lkn.1).

Pure numpy. Every interval uses the effective sample size (autocorr.tau_int), never raw n;
gaps contribute no pairs and are never interpolated. Closed forms instead of scipy:
Student-t quantiles by the Cornish-Fisher expansion (Abramowitz & Stegun 26.7.5), the
Kolmogorov distribution as its alternating series, KPSS critical values from the paper's table.
Design: docs/superpowers/specs/2026-10-02-series-diagnostics-design.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np

from telemetry_nerd.analysis.autocorr import ar1, n_eff, tau_int
from telemetry_nerd.analysis.fraction import wilson

ALPHA = 0.01
MIN_SEGMENT = 8
MAX_CHANGEPOINTS = 3
PHI_CAP = 0.99  # long-run variance sigma_e / (1 - phi) explodes near a random walk
MAD_SCALE = 1.4826  # MAD -> sigma for a normal
MAD_VAR = 1.3605  # n * var(sigma_MAD) / sigma^2 for a normal (MAD's 37% efficiency)
KPSS_LEVEL = ((0.10, 0.347), (0.05, 0.463), (0.025, 0.574), (0.01, 0.739))
BIMODAL_BC = 5 / 9
HOUR_MS = 3_600_000


def z_of(p: float) -> float:
    return NormalDist().inv_cdf(p)


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


def kolmogorov_sf(x: float) -> float:
    """P(sup |Brownian bridge| > x) = 2 Σ (−1)^(k−1) exp(−2 k² x²)."""
    if x <= 0.2:
        return 1.0
    k = np.arange(1, 101)
    return float(np.clip(2 * np.sum((-1.0) ** (k - 1) * np.exp(-2 * k * k * x * x)), 0.0, 1.0))


def robust_sigma(x: np.ndarray) -> float:
    return MAD_SCALE * float(np.median(np.abs(x - np.median(x))))


# seasonal ---------------------------------------------------------------------
def harmonic_design(t_s: np.ndarray, periods_s: list[float]) -> np.ndarray:
    cols = [np.ones_like(t_s, dtype=float)]
    for p in periods_s:
        w = 2 * np.pi * t_s / p
        cols += [np.sin(w), np.cos(w)]
    return np.column_stack(cols)


@dataclass(frozen=True)
class Harmonics:
    """Sinusoids at given periods fitted by least squares; `curve` excludes the intercept."""

    periods_s: list[float]
    coef: np.ndarray

    def curve(self, t_s: np.ndarray) -> np.ndarray:
        if not self.periods_s:
            return np.zeros_like(t_s, dtype=float)
        return harmonic_design(t_s, self.periods_s)[:, 1:] @ self.coef[1:]

    def amplitudes(self) -> list[float]:
        return [float(math.hypot(self.coef[1 + 2 * i], self.coef[2 + 2 * i])) for i in range(len(self.periods_s))]  # fmt: skip


def fit_harmonics(t_s: np.ndarray, y: np.ndarray, periods_s: list[float]) -> Harmonics:
    if not periods_s:
        return Harmonics([], np.array([float(np.mean(y))]))
    coef, *_ = np.linalg.lstsq(harmonic_design(t_s, periods_s), y, rcond=None)
    return Harmonics(list(periods_s), coef)


# trend ------------------------------------------------------------------------
@dataclass(frozen=True)
class Trend:
    slope_per_h: float
    interval_per_h: tuple[float, float]  # 1 - ALPHA, AR-adjusted
    change: float  # slope x range
    change_interval: tuple[float, float]
    tau: float  # of the residuals: SE inflation factor sqrt(tau)
    n_eff: float
    sigma_resid: float
    sse: float
    significant: bool


def trend(pos: np.ndarray, ts_ms: np.ndarray, y: np.ndarray, span_ms: int) -> Trend:
    t_h = (ts_ms - ts_ms[0]) / HOUR_MS
    tc = t_h - t_h.mean()
    sxx = float(tc @ tc)
    slope = float(tc @ (y - y.mean())) / sxx
    resid = y - y.mean() - slope * tc
    n = y.size
    tau = tau_int(pos, resid)
    ne = n_eff(n, tau)
    s2 = float(resid @ resid) / max(n - 2, 1)
    se = math.sqrt(s2 / sxx * tau)
    half = t_ppf(1 - ALPHA / 2, max(ne - 2, 1)) * se
    lo, hi = slope - half, slope + half
    span_h = span_ms / HOUR_MS
    return Trend(
        slope, (lo, hi), slope * span_h, (lo * span_h, hi * span_h), tau, ne,
        math.sqrt(s2), float(resid @ resid), not lo <= 0 <= hi,
    )  # fmt: skip


# level shifts -------------------------------------------------------------------
@dataclass(frozen=True)
class Shift:
    index: int  # first sample after the change
    ts_ms: int
    delta: float  # mean after - mean before (adjacent segments)
    interval: tuple[float, float]  # 1 - ALPHA
    p: float
    stat: float
    n_before: int
    n_after: int


def _split_cusum(y: np.ndarray) -> tuple[int, float]:
    s = np.cumsum(y - y.mean())[:-1]
    lo, hi = MIN_SEGMENT - 1, y.size - MIN_SEGMENT
    k = lo + int(np.argmax(np.abs(s[lo:hi])))
    return k + 1, float(abs(s[k]))


def _long_run_sigma(pos: np.ndarray, resid: np.ndarray) -> float:
    fit = ar1(pos, resid)
    phi = min(fit.phi, PHI_CAP)
    return fit.sigma_e / (1 - phi) if phi > 0 else fit.sigma_e


def _one_split(pos, ts_ms, y) -> Shift | None:
    n = y.size
    if n < 2 * MIN_SEGMENT:
        return None
    k, s = _split_cusum(y)
    a, b = y[:k], y[k:]
    resid = np.r_[a - a.mean(), b - b.mean()]
    slr = _long_run_sigma(pos, resid)
    if slr == 0:
        return None
    stat = s / (slr * math.sqrt(n))
    delta = float(b.mean() - a.mean())
    half = z_of(1 - ALPHA / 2) * slr * math.sqrt(1 / a.size + 1 / b.size)
    return Shift(
        k, int(ts_ms[k]), delta, (delta - half, delta + half), kolmogorov_sf(stat), stat,
        int(a.size), int(b.size),
    )  # fmt: skip


def changepoints(pos: np.ndarray, ts_ms: np.ndarray, y: np.ndarray) -> list[Shift]:
    """Binary segmentation with the CUSUM test (Kolmogorov null, long-run sigma from AR(1)
    residuals of the split model). At most MAX_CHANGEPOINTS, segments >= MIN_SEGMENT."""
    found: list[Shift] = []
    todo = [(0, y.size)]
    while todo and len(found) < MAX_CHANGEPOINTS:
        a, b = todo.pop(0)
        sh = _one_split(pos[a:b], ts_ms[a:b], y[a:b])
        if sh is None or sh.p >= ALPHA:
            continue
        k = a + sh.index
        found.append(Shift(k, sh.ts_ms, sh.delta, sh.interval, sh.p, sh.stat, sh.n_before, sh.n_after))  # fmt: skip
        todo += [(a, k), (k, b)]
    found.sort(key=lambda s: s.index)
    # re-estimate each delta between its neighbouring changepoints
    out = []
    bounds = [0, *[s.index for s in found], y.size]
    for i, sh in enumerate(found):
        before, after = y[bounds[i] : bounds[i + 1]], y[bounds[i + 1] : bounds[i + 2]]
        seg = slice(bounds[i], bounds[i + 2])
        resid = np.r_[before - before.mean(), after - after.mean()]
        slr = _long_run_sigma(pos[seg], resid)
        delta = float(after.mean() - before.mean())
        half = z_of(1 - ALPHA / 2) * slr * math.sqrt(1 / before.size + 1 / after.size)
        out.append(Shift(sh.index, sh.ts_ms, delta, (delta - half, delta + half), sh.p, sh.stat, int(before.size), int(after.size)))  # fmt: skip
    return out


# stationarity -------------------------------------------------------------------
@dataclass(frozen=True)
class KPSS:
    stat: float
    lags: int
    p_upper: float  # p is at most this (table bound); 0.1 means "> 10%"
    reject_5pct: bool


def kpss(y: np.ndarray) -> KPSS:
    """KPSS level-stationarity test (Kwiatkowski et al. 1992), Newey-West long-run variance."""
    n = y.size
    e = y - y.mean()
    s = np.cumsum(e)
    lags = int(12 * (n / 100) ** 0.25)
    lags = min(lags, n - 1)
    lrv = float(e @ e) / n
    for k in range(1, lags + 1):
        lrv += 2 * (1 - k / (lags + 1)) * float(e[k:] @ e[:-k]) / n
    stat = float(s @ s) / (n * n * lrv) if lrv > 0 else 0.0
    p = 0.1
    for level, crit in KPSS_LEVEL:
        if stat >= crit:
            p = level
    return KPSS(stat, lags, p, stat >= KPSS_LEVEL[1][1])


# variance --------------------------------------------------------------------------
@dataclass(frozen=True)
class VarianceRatio:
    ratio: float  # robust scale of the last third / the first third
    interval: tuple[float, float]  # 1 - ALPHA
    significant: bool


def variance_ratio(pos: np.ndarray, resid: np.ndarray) -> VarianceRatio | None:
    n = resid.size
    if n < 6 * MIN_SEGMENT:
        return None
    a, b = resid[: n // 3], resid[-(n // 3) :]
    sa, sb = robust_sigma(a), robust_sigma(b)
    if sa == 0 or sb == 0:
        return None
    na = n_eff(a.size, tau_int(pos[: n // 3], a))
    nb = n_eff(b.size, tau_int(pos[-(n // 3) :], b))
    r = sb / sa
    half = z_of(1 - ALPHA / 2) * math.sqrt(MAD_VAR / na + MAD_VAR / nb)
    lo, hi = r * math.exp(-half), r * math.exp(half)
    return VarianceRatio(r, (lo, hi), not lo <= 1 <= hi)


# shape ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Shape:
    n: int
    skew: float
    skew_interval: tuple[float, float]
    excess_kurtosis: float
    kurtosis_interval: tuple[float, float]
    zeros: int  # exact
    zero_share_interval: tuple[float, float]
    bimodality: float | None
    flags: list[str] = field(default_factory=list)


def _moments(y: np.ndarray) -> tuple[float, float]:
    d = y - y.mean(axis=-1, keepdims=True)
    m2 = np.mean(d**2, axis=-1)
    safe = np.where(m2 > 0, m2, 1.0)
    g1 = np.where(m2 > 0, np.mean(d**3, axis=-1) / safe**1.5, 0.0)
    g2 = np.where(m2 > 0, np.mean(d**4, axis=-1) / safe**2 - 3, 0.0)
    return g1, g2


def _block_bootstrap(y: np.ndarray, tau: float, reps: int, seed: int) -> np.ndarray:
    """Moving-block resamples (reps x n): blocks of ~2 tau keep the dependence."""
    n = y.size
    block = int(min(max(1, round(2 * tau)), max(1, n // 4)))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(reps, -(-n // block)))
    idx = (starts[:, :, None] + np.arange(block)).reshape(reps, -1)[:, :n]
    return y[idx]


def shape(y: np.ndarray, tau: float, reps: int = 400, seed: int = 0) -> Shape:
    """Skew / excess kurtosis with moving-block bootstrap percentile intervals (normal-theory
    SEs sqrt(6/n) are far too narrow for the skewed, heavy-tailed data telemetry is)."""
    n = y.size
    ne = n_eff(n, tau)
    g1, g2 = (float(v) for v in _moments(y))
    b1, b2 = _moments(_block_bootstrap(np.asarray(y, float), tau, reps, seed))
    a = ALPHA / 2
    s_int = (float(np.quantile(b1, a)), float(np.quantile(b1, 1 - a)))
    k_int = (float(np.quantile(b2, a)), float(np.quantile(b2, 1 - a)))
    zeros = int(np.sum(y == 0))
    z = z_of(1 - a)
    share = wilson(zeros / n * ne, ne, z) if zeros else (0.0, wilson(0, ne, z)[1])
    bc = (g1 * g1 + 1) / (g2 + 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))) if n > 3 else None
    flags = []
    if s_int[0] > 0.5 or s_int[1] < -0.5:
        flags.append("skewed")
    if k_int[0] > 1:
        flags.append("heavy_tails")
    if zeros and zeros / n > 0.1:
        flags.append("zero_inflated")
    # BC > 5/9 also fires for any strongly skewed unimodal law (exponential: 0.56); a balanced
    # two-mode mixture is platykurtic, so require negative excess kurtosis too
    if bc is not None and bc > BIMODAL_BC and g2 < 0:
        flags.append("bimodal")
    return Shape(n, g1, s_int, g2, k_int, zeros, share, bc, flags)


# periodicity against red noise --------------------------------------------------------
@dataclass(frozen=True)
class Period:
    period_s: float
    power: float  # GLS share of variance at this period, after removing stronger periods
    phi: float  # AR(1) of the background (residual after all candidate sinusoids)
    fap: float  # against the AR(1) background, over the M independent frequencies searched


def ar1_spectrum(f_hz: float, phi: float, step_s: float) -> float:
    """AR(1) spectral density relative to its mean (white = 1)."""
    return (1 - phi * phi) / (1 + phi * phi - 2 * phi * math.cos(2 * math.pi * f_hz * step_s))


def red_noise_test(
    pos: np.ndarray,
    t_s: np.ndarray,
    y: np.ndarray,
    candidates_s: list[float],
    step_s: float,
    n_freqs: float,
    background_s: list[float] | None = None,
) -> list[Period]:
    """FAP of each candidate period (strongest first) against an AR(1) red-noise background.

    phi comes from the residual after removing the `background_s` sinusoids (default: all
    candidates); each candidate is tested after prewhitening by the stronger candidates that
    passed (fap < ALPHA), so sidelobes of one sinusoid are not counted as more periods.
    Normalised GLS power p at f has p N / (2 S(f)) ~ Exp(1) under the background;
    FAP = 1 - (1 - e^-z)^M (Schulz & Mudelsee's REDFIT idea, closed form)."""
    from telemetry_nerd.analysis.autocorr import ar1
    from telemetry_nerd.analysis.spectrum import lomb_scargle

    if not candidates_s:
        return []
    bg = candidates_s if background_s is None else background_s
    full = y - fit_harmonics(t_s, y, bg).curve(t_s)
    phi = max(0.0, ar1(pos, full - full.mean()).phi)
    out: list[Period] = []
    n = y.size
    for p_s in candidates_s:
        kept = [k.period_s for k in out if k.fap < ALPHA]
        r = y - fit_harmonics(t_s, y, kept).curve(t_s)
        f = 1.0 / p_s
        power = float(lomb_scargle(t_s, r - r.mean(), np.array([f]))[0])
        z = power * n / (2 * ar1_spectrum(f, phi, step_s))
        fap = float(-np.expm1(n_freqs * np.log1p(-math.exp(-z)))) if z < 700 else 0.0
        out.append(Period(p_s, power, phi, fap))
    return out


def confirm_periods(
    pos: np.ndarray,
    t_s: np.ndarray,
    y: np.ndarray,
    candidates_s: list[float],
    step_s: float,
    n_freqs: float,
) -> list[Period]:
    """Keep candidate periods that stand out from an AR(1) red-noise background.

    The white-noise FAP (Baluev) calls any long-period wandering significant; see
    `red_noise_test` for the background model and prewhitening."""
    return [p for p in red_noise_test(pos, t_s, y, candidates_s, step_s, n_freqs) if p.fap < ALPHA]
