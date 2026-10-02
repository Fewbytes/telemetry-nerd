"""Fleet analysis: spread across members per step and outlying members (bead lkn.3). Pure numpy.

A fleet is a matrix Y (members x steps) on one step grid; NaN = the member did not report. The
per-step spread is descriptive (the fleet is the population at that step), computed over the
members that reported. Outliers are judged against the OTHER members (leave-one-out median/MAD):
level and change (trimmed means of a member's deviations, compared across members) and
excursions at three durations (spike, 5- and 15-step episodes: rolling medians of AR(1)-
prewhitened deviations), Bonferroni over members x tests
(x steps for excursions), family-wise 1%. Nothing is interpolated or imputed.
Design: docs/superpowers/specs/2026-10-02-fleet-analysis-design.md.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

import numpy as np

from telemetry_nerd.analysis.autocorr import n_eff, tau_int

MIN_MEMBERS = 5  # fewer: draw the members as lines / small multiples
MIN_TESTED = 10  # members with enough data for the outlier tests
LOO_MAX = (
    64  # exact leave-one-out up to this many members; beyond, one member moves median/MAD O(1/n)
)
MIN_OTHERS = 4  # other members reporting at a step for a z there
MIN_OBS = 10  # z values a member needs to be tested
POOL_HALF = 6  # per-step sigma pooled over +-6 steps
TAIL_POINTS = (1e-3, 1e-4)  # two-sided tail points of the heavy-tail check
TRIM = 0.2  # level/change: 20% trimmed means over time (see design: medians are miscalibrated)
EXCURSIONS = {"spike": 1, "episode": 5, "long_episode": 15}  # rolling-median widths (steps)
# a rolling median over w prewhitened steps is exceeded only when ceil(w / 2 tau_r) roughly
# independent values are, so its tail index is that multiple of a single step's: with fewer than
# MIN_EXCEEDING it inherits the single-step heavy-tail verdict (5 steps: 3; 15 steps: 8)
MIN_EXCEEDING = 5
ALPHA = 0.01  # family-wise, over members x tests (x steps)
N_TESTS = 3
DF_FACTOR = 0.3675  # MAD -> t df: 1 / (2 * 1.3605), MAD's 37% efficiency (calibrated in tests)
MAD_SCALE = 1.4826
MEANAD_SCALE = 1.2533  # mean absolute deviation -> sigma for a normal
SINCE_Z = 2.0
MANY_OUTLIERS = 0.10
SPREAD_MIN_N = {"q25": 5, "q10": 10}
# Croux & Rousseeuw (1992) small-sample consistency factors for the MAD
_MAD_SMALL = {2: 1.196, 3: 1.495, 4: 1.363, 5: 1.206, 6: 1.200, 7: 1.140, 8: 1.129, 9: 1.107}

Scale = Literal["log", "linear"]
Normalise = Literal["none", "member"]
Kind = Literal["persistent", "drifting", "shifted", "transient"]


# Student t without scipy -----------------------------------------------------------------
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


def mad_factor(n: int | np.ndarray) -> np.ndarray:
    n = np.asarray(n)
    big = np.where(n > 9, n / np.maximum(n - 0.8, 1e-9), 1.0)
    small = np.vectorize(lambda k: _MAD_SMALL.get(int(k), 1.0), otypes=[float])(n)
    return np.where(n > 9, big, small)


# robust centre / scale ---------------------------------------------------------------------
def _pooled(others: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per step: median of the members, and a robust sigma of their deviations from the per-step
    median pooled over +-POOL_HALF steps (local heteroscedasticity; far more values than one
    step's members, so the z threshold is not dominated by MAD noise). Returns (centre, sigma,
    values pooled); NaN where fewer than MIN_OTHERS members report."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        n = np.sum(~np.isnan(others), axis=0)
        c = np.nanmedian(others, axis=0)
        dev = others - c
        w = 2 * POOL_HALF + 1
        pad = np.pad(dev, ((0, 0), (POOL_HALF, POOL_HALF)), constant_values=np.nan)
        win = np.lib.stride_tricks.sliding_window_view(pad, w, axis=1)  # (n_o, T, w)
        flat = np.moveaxis(win, 1, 0).reshape(dev.shape[1], -1)
        cnt = np.sum(~np.isnan(flat), axis=1)
        mad = np.nanmedian(np.abs(flat), axis=1) * MAD_SCALE * mad_factor(cnt)
        meanad = np.nanmean(np.abs(flat), axis=1) * MEANAD_SCALE
    s = np.where(mad > 0, mad, meanad)
    bad = (n < MIN_OTHERS) | ~(s > 0)
    return np.where(bad, np.nan, c), np.where(bad, np.nan, s), np.where(bad, 0, cnt)


def loo_deviations(y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, bool]:
    """d_it = y_it - median of the other members at t; z_it = d_it / their pooled robust sigma.
    Exact leave-one-out for <= LOO_MAX members; the full fleet beyond (stated).
    Also returns the number of deviations behind each sigma."""
    m_ = y.shape[0]
    if m_ <= LOO_MAX:
        d, s, cnt = np.full_like(y, np.nan), np.full_like(y, np.nan), np.zeros(y.shape)
        for i in range(m_):
            c, sc, k = _pooled(np.delete(y, i, axis=0))
            d[i], s[i], cnt[i] = y[i] - c, sc, k
        loo = True
    else:
        c, sc, k = _pooled(y)
        d = y - c
        s, cnt = np.broadcast_to(sc, y.shape), np.broadcast_to(k, y.shape)
        loo = False
    with np.errstate(invalid="ignore", divide="ignore"):
        z = d / s
    return d, z, cnt, loo


def typical_tau(d: np.ndarray, sample: int = 40) -> float:
    """Median autocorrelation time of members' deviation series (a spread-out sample)."""
    taus = []
    for i in np.unique(np.linspace(0, d.shape[0] - 1, min(sample, d.shape[0])).astype(int)):
        ok = np.flatnonzero(~np.isnan(d[i]))
        if ok.size >= MIN_OBS:
            x = d[i, ok]
            taus.append(tau_int(ok - ok[0], x - np.median(x)))
    return float(np.median(taus)) if taus else 1.0


def loo_centre_scale(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per element: median and robust sigma (small-sample MAD, mean-AD fallback) of the others."""
    k = v.size
    m, s = np.empty(k), np.empty(k)
    for i in range(k):
        o = np.delete(v, i)
        m[i] = float(np.median(o))
        ad = np.abs(o - m[i])
        si = float(np.median(ad)) * MAD_SCALE * float(mad_factor(k - 1))
        s[i] = si if si > 0 else float(np.mean(ad)) * MEANAD_SCALE
    return m, s


def loo_robust_z(v: np.ndarray) -> np.ndarray:
    """Robust z of each value against the others."""
    m, s = loo_centre_scale(v)
    diff = v - m
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(s > 0, diff / np.where(s > 0, s, 1.0), np.sign(diff) * np.inf)
    return np.where(diff == 0, 0.0, z)


# results ------------------------------------------------------------------------------------
@dataclass
class Spread:
    n: np.ndarray  # members reporting per step
    alive: np.ndarray  # members seen at or before the step
    median: np.ndarray
    q25: np.ndarray
    q75: np.ndarray
    q10: np.ndarray
    q90: np.ndarray
    lo: np.ndarray  # min
    hi: np.ndarray  # max


@dataclass
class Episode:
    start: int  # step index, inclusive
    end: int  # inclusive
    peak_z: float
    sustained: bool


@dataclass
class Outlier:
    member: int
    kind: Kind
    direction: Literal["higher", "lower"]
    score: float
    fired: list[str]
    z: dict[str, float]  # level/change: across-member robust z; excursions: max |z| at that scale
    since: int | None  # step index
    since_window_start: bool
    offset: float  # trimmed mean d over the window (log ratio or difference)
    offset_interval: tuple[float, float]
    change: float
    change_interval: tuple[float, float]
    at: int | None = None  # shifted: step index of the best split
    peak_at: int | None = None
    episodes: list[Episode] = field(default_factory=list)
    tau: float = 1.0
    n: int = 0


@dataclass
class Fleet:
    scale: Scale
    normalise: Normalise
    spread: Spread
    loo: bool
    tested: list[int]
    untested: list[int]
    outliers: list[Outlier]
    thresholds: dict[str, float]
    caveats: list[str]
    first_seen: np.ndarray  # step index, -1 never
    last_seen: np.ndarray
    z: np.ndarray  # members x steps deviations in fleet-sigma units
    d: np.ndarray  # members x steps deviations in scale units (log ratio or difference)
    values: np.ndarray  # members x steps in the band's units (raw, or relative to own median)


def choose_scale(y: np.ndarray, scale: str = "auto") -> Scale:
    if scale in ("log", "linear"):
        if scale == "log" and not np.all(y[~np.isnan(y)] > 0):
            raise ValueError("log scale needs every value > 0 (hint: scale='linear')")
        return scale  # type: ignore[return-value]
    if scale != "auto":
        raise ValueError(f"scale must be auto, log or linear, got {scale!r}")
    obs = y[~np.isnan(y)]
    return "log" if obs.size and np.all(obs > 0) else "linear"


def spread(y: np.ndarray) -> Spread:
    obs = ~np.isnan(y)
    n = obs.sum(axis=0)
    seen = np.maximum.accumulate(obs, axis=1)
    alive = seen.sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        q = np.nanquantile(y, [0.1, 0.25, 0.5, 0.75, 0.9], axis=0)
        lo, hi = np.nanmin(y, axis=0), np.nanmax(y, axis=0)

    def gate(a: np.ndarray, k: int) -> np.ndarray:
        return np.where(n >= k, a, np.nan)

    return Spread(
        n=n, alive=alive, median=gate(q[2], 3),
        q25=gate(q[1], SPREAD_MIN_N["q25"]), q75=gate(q[3], SPREAD_MIN_N["q25"]),
        q10=gate(q[0], SPREAD_MIN_N["q10"]), q90=gate(q[4], SPREAD_MIN_N["q10"]),
        lo=gate(lo, 2), hi=gate(hi, 2),
    )  # fmt: skip


def _runs(mask: np.ndarray, max_gap: int) -> list[tuple[int, int]]:
    """Runs of True, merging runs separated by <= max_gap False steps."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    out = [[int(idx[0]), int(idx[0])]]
    for i in idx[1:]:
        if i - out[-1][1] - 1 <= max_gap:
            out[-1][1] = int(i)
        else:
            out.append([int(i), int(i)])
    return [(a, b) for a, b in out]


def _since(z: np.ndarray, sign: float, tau: float) -> tuple[int | None, bool]:
    """Start of the final stretch where the rolling median z stays beyond SINCE_Z in `sign`."""
    w = max(5, math.ceil(2 * tau))
    r = rolling_median(z[None, :], w)[0] * sign
    valid = np.flatnonzero(~np.isnan(r))
    if valid.size == 0 or not r[valid[-1]] > SINCE_Z:
        return None, False
    start = int(valid[-1])
    for i in valid[::-1]:
        if r[i] > SINCE_Z:
            start = int(i)
        else:
            break
    return start, start == int(valid[0])


def trimmed_interval(dev: np.ndarray, tau: float) -> tuple[float, tuple[float, float]]:
    """20% trimmed mean of an autocorrelated series with a 99% interval: winsorized sd /
    ((1 - 2 trim) sqrt(n_eff)), t with n_eff - 1 df (Tukey-McLaughlin, n -> n / tau)."""
    v = np.sort(dev)
    c = int(v.size * TRIM)
    m = float(v[c : v.size - c].mean())
    wins = np.clip(v, v[c], v[v.size - 1 - c])
    ne = n_eff(v.size, tau)
    se = float(np.std(wins, ddof=1)) / ((1 - 2 * TRIM) * math.sqrt(max(ne, 1.0)))
    half = t_isf(0.005, max(ne - 1, 1.0)) * se
    return m, (m - half, m + half)


def _step_or_trend(pos: np.ndarray, d: np.ndarray) -> tuple[str, int | None]:
    """'gradual' when a line fits the deviation better than one step, else ('step', split)."""
    n = d.size
    a = np.vstack([pos, np.ones(n)]).T
    coef, *_ = np.linalg.lstsq(a, d, rcond=None)
    sse_line = float(np.sum((d - a @ coef) ** 2))
    cs, cs2 = np.cumsum(d), np.cumsum(d * d)
    best, at = math.inf, None
    for k in range(3, n - 2):
        left = cs2[k - 1] - cs[k - 1] ** 2 / k
        rs, rs2 = cs[-1] - cs[k - 1], cs2[-1] - cs2[k - 1]
        right = rs2 - rs**2 / (n - k)
        if left + right < best:
            best, at = float(left + right), k
    if at is None or sse_line <= best:
        return "gradual", None
    return "step", at


def analyse(
    y_raw: np.ndarray, scale: str = "auto", normalise: Normalise = "none", alpha: float = ALPHA
) -> Fleet:
    """Spread, per-step deviations and outlying members of a fleet (members x steps, NaN gaps)."""
    m_, t_ = y_raw.shape
    if m_ < MIN_MEMBERS:
        raise ValueError(f"{m_} members: a fleet needs >= {MIN_MEMBERS} (hint: draw them as lines)")
    if normalise not in ("none", "member"):
        raise ValueError(f"normalise must be none or member, got {normalise!r}")
    sc = choose_scale(y_raw, scale)
    obs = ~np.isnan(y_raw)
    first = np.where(obs.any(axis=1), obs.argmax(axis=1), -1)
    last = np.where(obs.any(axis=1), t_ - 1 - obs[:, ::-1].argmax(axis=1), -1)
    y = np.log(y_raw) if sc == "log" else y_raw.astype(float)
    if normalise == "member":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            y = y - np.nanmedian(y, axis=1, keepdims=True)
    # the band in the units the user reads: raw, or normalised (ratio / difference to own median)
    band_y = y_raw if normalise == "none" else (np.exp(y) if sc == "log" else y)
    sp = spread(band_y)
    d, z, cnt, loo = loo_deviations(y)
    caveats: list[str] = []
    alive_steps = np.array([t_ - f if f >= 0 else 0 for f in first])
    nz = np.sum(~np.isnan(z), axis=1)
    tested = [i for i in range(m_) if nz[i] >= MIN_OBS and nz[i] >= 0.5 * alive_steps[i]]
    untested = [i for i in range(m_) if i not in tested]
    thresholds: dict[str, float] = {}
    outliers: list[Outlier] = []
    if len(tested) < MIN_TESTED:
        caveats.append("too_few_members_for_outliers")
        return Fleet(sc, normalise, sp, loo, tested, untested, [], thresholds, caveats,
                     first, last, z, d, band_y)  # fmt: skip
    if untested:
        caveats.append("members_skipped")
    k = len(tested)
    a_test = alpha / N_TESTS
    thr = t_isf(a_test / (2 * k), DF_FACTOR * (k - 1))
    thresholds["member"] = thr
    zt = z[tested]
    third = max(1, t_ // 3)
    level = trimmed_mean(zt)
    change = trimmed_mean(zt[:, t_ - third :]) - trimmed_mean(zt[:, :third])
    tests: dict[str, np.ndarray] = {}
    if normalise == "none":
        tests["level"] = loo_robust_z(level)
    ok_change = ~np.isnan(change)
    zc = np.zeros(k)
    if ok_change.sum() >= MIN_TESTED:
        zc[ok_change] = loo_robust_z(change[ok_change])
    tests["change"] = zc
    flagged_lc = np.zeros(k, bool)
    for v in tests.values():
        flagged_lc |= np.abs(v) > thr
    tau_hat = typical_tau(d[tested])
    thresholds["tau"] = tau_hat
    # excursions at three durations, alpha split: single steps (spikes) and rolling medians over
    # 5 and 15 steps (episodes: most of the window out, so heavy-tailed single-step noise cannot
    # fake them). Light tails: Student t with the pooled sigma's df, Bonferroni over every
    # member-step. Heavy tails at a scale (the typical member exceeds t's 0.1% point too often):
    # each member's peak also against a Gumbel fitted to the other members' peaks.
    w = 2 * POOL_HALF + 1
    df = np.maximum(DF_FACTOR * (cnt[tested] / min(max(tau_hat, 1.0), w) - 1), 1.0)
    bars: dict[str, np.ndarray] = {}
    # episodes are scanned on AR(1)-prewhitened deviations: under heavy-tailed noise one huge
    # innovation decays over several steps and can hold a rolling median of raw z up (the
    # 15-step scale's false alarms under t(3), lkn.14); innovations do not. A sustained shift
    # delta becomes (1 - phi) delta against innovation noise sd sqrt(1 - phi^2) sigma: for the
    # 15-step median about the same power as raw z at phi = 0.6.
    calm = ~flagged_lc
    phi = fleet_phi(zt[calm]) if calm.any() else 0.0
    thresholds["phi"] = phi
    ep_in = prewhiten(zt, phi)
    tau_ep = typical_tau(ep_in[calm]) if calm.any() else 1.0
    thresholds["tau_prewhitened"] = tau_ep
    scans = {name: zt if w_ == 1 else rolling_median(ep_in, w_) for name, w_ in EXCURSIONS.items()}
    n_scales = len(EXCURSIONS)
    spike_heavy = False
    for name, zz in scans.items():
        n_all = max(int(np.sum(np.isfinite(zz))), 1)
        p = a_test / n_scales / n_all
        if name == "spike":
            unit = np.ones(zz.shape)
        else:  # the rolling median's own scale relative to single steps (pool members)
            pool = zz[~flagged_lc]
            ratio = float(np.nanmedian(np.abs(pool))) / float(np.nanmedian(np.abs(zt[~flagged_lc])))
            unit = np.full(zz.shape, ratio)
        tdf = np.vectorize(lambda v, q=p: t_isf(q / 2, float(round(v, 1))), otypes=[float])(df)
        bar = tdf * unit
        thresholds[f"{name}_threshold"] = float(np.median(bar))
        # a median needing few independent exceedances keeps the single-step tails
        own = heavy_tailed(zz / unit, ~flagged_lc, float(np.median(df)), tau_hat)
        if name == "spike":
            spike_heavy = own
        inherits = math.ceil(EXCURSIONS[name] / (2 * max(tau_ep, 1.0))) < MIN_EXCEEDING
        if own or (spike_heavy and inherits):
            thresholds[f"{name}_heavy_tails"] = 1.0
            if "heavy_tailed_noise" not in caveats:
                caveats.append("heavy_tailed_noise")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                peaks = np.nanmax(np.abs(zz), axis=1)
            gb = gumbel_bars(peaks, ~flagged_lc, a_test / n_scales / k)
            thresholds[f"{name}_tail_threshold"] = (
                float(np.median(gb[np.isfinite(gb)])) if np.isfinite(gb).any() else math.inf
            )
            bar = np.maximum(bar, gb[:, None])
        bars[name] = bar
    for row, i in enumerate(tested):
        fired: list[str] = []
        zs: dict[str, float] = {}
        for name, v in tests.items():
            zs[name] = float(v[row])
            if abs(v[row]) > thr:
                fired.append(name)
        exceed = np.zeros(t_, bool)
        ratio = 0.0
        for name, zz_all in scans.items():
            zz = zz_all[row]
            with np.errstate(invalid="ignore"):
                ex = np.abs(zz) > bars[name][row]
                r = np.abs(zz) / bars[name][row]
            zs[name] = float(np.nanmax(np.abs(zz))) if np.isfinite(zz).any() else 0.0
            if ex.any():
                fired.append(name)
                exceed |= ex
                ratio = max(ratio, float(np.nanmax(r)))
        if fired:
            outliers.append(_describe(i, z[i], d[i], fired, zs, exceed, ratio, thr, t_, tau_hat))
    if len(outliers) > MANY_OUTLIERS * k:
        caveats.append("many_outliers")
    outliers.sort(key=lambda o: -o.score)
    return Fleet(sc, normalise, sp, loo, tested, untested, outliers, thresholds, caveats,
                 first, last, z, d, band_y)  # fmt: skip


def fleet_phi(z: np.ndarray) -> float:
    """Median over members of the lag-1 autocorrelation of their deviations (median removed)."""
    out = []
    for row in z:
        x = row - np.nanmedian(row) if np.isfinite(row).sum() >= MIN_OBS else None
        if x is None:
            continue
        a, b = x[1:], x[:-1]
        ok = np.isfinite(a) & np.isfinite(b)
        if ok.sum() >= MIN_OBS:
            den = math.sqrt(float(np.sum(a[ok] ** 2)) * float(np.sum(b[ok] ** 2)))
            if den > 0:
                out.append(float(np.sum(a[ok] * b[ok])) / den)
    return float(np.clip(np.median(out), 0.0, 0.95)) if out else 0.0


def prewhiten(z: np.ndarray, phi: float) -> np.ndarray:
    """AR(1) innovations of z in z's marginal units: (z_t - phi z_{t-1}) / sqrt(1 - phi^2);
    NaN at the first step and after a gap."""
    r = np.full(z.shape, np.nan)
    r[:, 1:] = (z[:, 1:] - phi * z[:, :-1]) / math.sqrt(1 - phi * phi)
    return r


def trimmed_mean(x: np.ndarray, trim: float = TRIM) -> np.ndarray:
    """Row-wise mean of the observed values after dropping `trim` of them at each end."""
    out = np.full(x.shape[0], np.nan)
    for r in range(x.shape[0]):
        v = np.sort(x[r][np.isfinite(x[r])])
        if v.size:
            c = int(v.size * trim)
            out[r] = float(v[c : v.size - c].mean())
    return out


def rolling_median(x: np.ndarray, w: int) -> np.ndarray:
    """Centred rolling median per row; NaN with fewer than ceil(w/2) observed values."""
    h = w // 2
    pad = np.pad(x, ((0, 0), (h, h)), constant_values=np.nan)
    win = np.lib.stride_tricks.sliding_window_view(pad, w, axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(win, axis=-1)
    return np.where(np.sum(np.isfinite(win), axis=-1) >= math.ceil(w / 2), med, np.nan)


def poisson_sf(x: int, lam: float) -> float:
    """P(X >= x) for X ~ Poisson(lam)."""
    if x <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for k in range(1, x):
        term *= lam / k
        cdf += term
    return max(0.0, 1.0 - cdf)


def heavy_tailed(z: np.ndarray, pool_rows: np.ndarray, df: float, tau_hat: float) -> bool:
    """Does the typical member exceed the two-sided 0.1% or 0.01% points of t_df more often than
    t_df says? Exceedance shares are 20% trimmed means over members (a few faulty members cannot
    trip it); Poisson test at 0.1% per point on the values pooled (dependence between
    neighbouring steps makes this trigger more often, which only makes the bar conservative)."""
    a = np.abs(z[pool_rows])
    if not a.size:
        return False
    ne = max((1 - 2 * TRIM) * float(np.sum(np.isfinite(a))), 1.0)
    n_row = np.maximum(np.sum(np.isfinite(a), axis=1), 1)
    for q in TAIL_POINTS:
        x = t_isf(q / 2, df)
        with np.errstate(invalid="ignore"):
            shares = np.sum(a > x, axis=1) / n_row
        share = float(trimmed_mean(shares[None, :])[0])
        if poisson_sf(round(share * ne), q * ne) < 0.001:
            return True
    return False


def gumbel_noise(n: int, g: float) -> float:
    """Sampling variance, in beta^2 units, of the Gumbel quantile at reduced variate g when mu and
    beta come from the quartiles of n values (delta method, asymptotic order-statistic
    covariances; standard Gumbel density at the u-quantile is -u ln u)."""
    a, b = 0.25, 0.75
    ga, gb = -math.log(-math.log(a)), -math.log(-math.log(b))
    fa, fb = -a * math.log(a), -b * math.log(b)
    c = (g - ga) / (gb - ga)  # x_hat = (1 - c) q_a + c q_b
    v = (1 - c) ** 2 * a * (1 - a) / fa**2 + c * c * b * (1 - b) / fb**2
    v += 2 * c * (1 - c) * a * (1 - b) / (fa * fb)
    return v / n


def gumbel_bars(peaks: np.ndarray, pool_rows: np.ndarray, p: float) -> np.ndarray:
    """Per member: the peak exceeded with probability p by a Gumbel fitted to the OTHER (pool)
    members' log peaks by their quartiles (beta = IQR / 1.5725, mu = q25 + 0.3266 beta).
    The log of a maximum is Gumbel-like for normal and for Frechet (heavy) tails alike.
    Extrapolating from n peaks to p ~ 1e-5 is noisy (sd ~ 1.5 beta at n = 100) and a noisy bar
    is exceeded more often on average (convexity: E[p e^-eps] = p e^(s^2/2)), so the bar is
    raised by beta s^2 / 2: the false alarm rate averaged over the fit's noise is then p."""
    g = -math.log(-math.log1p(-p))
    lp = np.log(np.maximum(peaks, 1e-12))
    out = np.empty(peaks.size)
    for i in range(peaks.size):
        keep = pool_rows.copy()
        keep[i] = False
        o = lp[keep & np.isfinite(lp)]
        if o.size < MIN_TESTED - 1:
            out[i] = math.inf
            continue
        q25, q75 = np.quantile(o, [0.25, 0.75])
        beta = (q75 - q25) / 1.5725
        out[i] = math.exp(q25 + 0.3266 * beta + beta * (g + gumbel_noise(o.size, g) / 2))
    return out


def _describe(i, zi, di, fired, zs, exceed, ratio, thr, t_, tau_fleet) -> Outlier:
    ok = ~np.isnan(zi)
    pos = np.flatnonzero(ok)
    dv = di[ok]
    calm = ok & ~exceed
    bpos = np.flatnonzero(calm) if calm.sum() >= MIN_OBS else pos
    # noise autocorrelation: calm steps, a linear trend removed (a drift is the signal, not
    # noise), never below the fleet's typical tau
    base = zi[bpos]
    coef = np.polyfit(bpos.astype(float), base, 1)
    tau = max(tau_int(bpos - bpos[0], base - np.polyval(coef, bpos)), tau_fleet)
    third = max(1, t_ // 3)
    off, lo_i = trimmed_interval(dv, tau)
    first_d, last_d = di[:third], di[t_ - third :]
    first_d, last_d = first_d[~np.isnan(first_d)], last_d[~np.isnan(last_d)]
    if first_d.size >= 3 and last_d.size >= 3:
        m1, (a1, _) = trimmed_interval(first_d, tau)
        m2, (a2, _) = trimmed_interval(last_d, tau)
        chg = m2 - m1
        half = math.hypot(m1 - a1, m2 - a2)
        chg_i = (chg - half, chg + half)
    else:
        chg, chg_i = 0.0, (-math.inf, math.inf)
    scores = [abs(zs[f]) / thr for f in fired if f in ("level", "change")]
    score = max([*scores, ratio])
    episodes: list[Episode] = []
    at = None
    peak_at = int(np.nanargmax(np.abs(np.where(ok, zi, 0.0))))
    if "change" in fired:
        pattern, split = _step_or_trend(pos.astype(float), dv)
        kind: Kind = "shifted" if pattern == "step" else "drifting"
        at = int(pos[split]) if split is not None else None
        sign = math.copysign(1.0, zs["change"])
    elif "level" in fired:
        kind = "persistent"
        sign = math.copysign(1.0, zs["level"])
    else:
        kind = "transient"
        sign = math.copysign(1.0, float(zi[peak_at]))
        for a, b in _runs(exceed, max(0, math.ceil(tau) - 1)):
            seg = np.where(np.isnan(zi[a : b + 1]), 0.0, zi[a : b + 1])
            j = int(np.argmax(np.abs(seg)))
            episodes.append(Episode(a, b, float(seg[j]), b - a + 1 > tau))
    since, from_start = _since(zi, sign, tau)
    if kind == "transient" and episodes:
        since, from_start = None, False
    return Outlier(
        member=i, kind=kind, direction="higher" if sign > 0 else "lower", score=score,
        fired=fired, z=zs, since=since, since_window_start=from_start, offset=off,
        offset_interval=lo_i, change=chg, change_interval=chg_i, at=at, peak_at=peak_at,
        episodes=episodes, tau=tau, n=int(ok.sum()),
    )  # fmt: skip
