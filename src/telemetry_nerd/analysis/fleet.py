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
from typing import Literal

import numpy as np

from telemetry_nerd.analysis.autocorr import n_eff, tau_int
from telemetry_nerd.analysis.stats import MAD_SCALE, binom_sf, poisson_sf, t_isf

MIN_MEMBERS = 5  # fewer: draw the members as lines / small multiples
MIN_TESTED = 10  # members with enough data for the outlier tests
LOO_MAX = (
    64  # exact leave-one-out up to this many members; beyond, one member moves median/MAD O(1/n)
)
MIN_OTHERS = 4  # other members reporting at a step for a z there
MIN_OBS = 10  # z values a member needs to be tested
POOL_HALF = 6  # per-step sigma pooled over +-6 steps
POOL_CELLS = 4_000_000  # values held at once while pooling (memory bound)
MAX_BAND_WINDOW = 121  # widest drawn band pool (steps)
P_BEYOND_3 = math.erfc(3 / math.sqrt(2))  # 0.27%: a normal value beyond +-3 sigma
WIDENING_ALPHA = 0.01  # family-wise over the window's steps: chance of any widening mark
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
MEANAD_SCALE = 1.2533  # mean absolute deviation -> sigma for a normal
SINCE_Z = 2.0
MANY_OUTLIERS = 0.10
SPREAD_MIN_N = {"q25": 5, "q10": 10}
# Croux & Rousseeuw (1992) small-sample consistency factors for the MAD
_MAD_SMALL = {2: 1.196, 3: 1.495, 4: 1.363, 5: 1.206, 6: 1.200, 7: 1.140, 8: 1.129, 9: 1.107}

Scale = Literal["log", "linear"]
Normalise = Literal["none", "member"]
Kind = Literal["persistent", "drifting", "shifted", "transient"]


def mad_factor(n: int | np.ndarray) -> np.ndarray:
    n = np.asarray(n)
    big = np.where(n > 9, n / np.maximum(n - 0.8, 1e-9), 1.0)
    small = np.vectorize(lambda k: _MAD_SMALL.get(int(k), 1.0), otypes=[float])(n)
    return np.where(n > 9, big, small)


# robust centre / scale ---------------------------------------------------------------------
def _pooled(others: np.ndarray, half: int = POOL_HALF) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per step: median of the members, and a robust sigma of their deviations from the per-step
    median pooled over +-half steps (local heteroscedasticity; far more values than one
    step's members, so the z threshold is not dominated by MAD noise). Returns (centre, sigma,
    values pooled); NaN where fewer than MIN_OTHERS members report."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        n = np.sum(~np.isnan(others), axis=0)
        c = np.nanmedian(others, axis=0)
        dev = others - c
        w = 2 * half + 1
        pad = np.abs(np.pad(dev, ((0, 0), (half, half)), constant_values=np.nan))
        t_ = dev.shape[1]
        med, mean, cnt = np.full(t_, np.nan), np.full(t_, np.nan), np.zeros(t_, int)
        # blocks of steps, so the windows never hold more than ~4M values (wide band_window)
        blk = max(1, POOL_CELLS // max(1, dev.shape[0] * w))
        for a in range(0, t_, blk):
            b = min(t_, a + blk)
            win = np.lib.stride_tricks.sliding_window_view(pad[:, a : b + 2 * half], w, axis=1)
            flat = np.moveaxis(win, 1, 0).reshape(b - a, -1)  # (steps, members x w)
            cnt[a:b] = np.sum(~np.isnan(flat), axis=1)
            med[a:b] = np.nanmedian(flat, axis=1)
            mean[a:b] = np.nanmean(flat, axis=1)
        mad = med * MAD_SCALE * mad_factor(cnt)
        meanad = mean * MEANAD_SCALE
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
    # level / change outliers: an excursion beyond the member's own (shifted, drifting) level,
    # peak_z in units of the fleet sigma relative to that level, not to the fleet centre
    beyond_own_level: bool = False


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


def spread(y: np.ndarray, unknown: np.ndarray | None = None) -> Spread:
    """Per-step spread over the members that reported. `unknown` (members x steps, bucket_state
    UNKNOWN: fetch failed, source cannot tell) cells are neither reporting nor missing: they
    leave n and alive alike."""
    obs = ~np.isnan(y)
    n = obs.sum(axis=0)
    seen = np.maximum.accumulate(obs, axis=1)
    if unknown is not None:
        seen = seen & ~unknown
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


QUANTILES = {"q10": 0.1, "q25": 0.25, "median": 0.5, "q75": 0.75, "q90": 0.9}


def _hf7(x: np.ndarray, h: float) -> float:
    """Hyndman-Fan 7 at 0-based position h of sorted x (which may hold -inf / +inf)."""
    lo = math.floor(h)
    frac = h - lo
    if frac < 1e-12:
        return float(x[lo])
    a, b = float(x[lo]), float(x[lo + 1])
    if math.isinf(a) or math.isinf(b):
        return a if math.isinf(a) else b
    return a + frac * (b - a)


def missing_bounds(
    y: np.ndarray, n: np.ndarray, alive: np.ndarray, gone: np.ndarray | None = None
) -> dict[str, np.ndarray]:
    """Missing-member bounds of the per-step quantiles: at a step where m = alive - n - gone
    members did not report (`gone`: members the source marked stale, counted from their stale
    point: a positive observation that they ended, so they have no value to bound), each quantile of all alive members is recomputed with the m missing values at
    -inf (lower bound) and at +inf (upper bound). The bound is -inf / +inf when the quantile's
    order statistics reach into the missing ones (m large enough for that rank): unbounded.
    Where nobody is missing the bounds equal the quantile. Gated like the spread (n per step).
    Returns {name_lo, name_hi} per quantile; NaN where the quantile itself is not drawn."""
    t_ = y.shape[1]
    out = {f"{k}_{s}": np.full(t_, np.nan) for k in QUANTILES for s in ("lo", "hi")}
    gates = {"median": 3, "q25": SPREAD_MIN_N["q25"], "q75": SPREAD_MIN_N["q25"],
             "q10": SPREAD_MIN_N["q10"], "q90": SPREAD_MIN_N["q10"]}  # fmt: skip
    for t in range(t_):
        x = np.sort(y[:, t][~np.isnan(y[:, t])])
        g = 0 if gone is None else int(gone[t])
        k, m = x.size, max(int(alive[t]) - int(n[t]) - g, 0)
        low = np.concatenate([np.full(m, -np.inf), x])
        high = np.concatenate([x, np.full(m, np.inf)])
        for name, q in QUANTILES.items():
            if k < gates[name]:
                continue
            h = (k + m - 1) * q
            out[f"{name}_lo"][t] = _hf7(low, h)
            out[f"{name}_hi"][t] = _hf7(high, h)
    return out


@dataclass
class ControlBand:
    """The fleet's SPC reference band, in the band's units (raw, or relative to own median):
    centre = per-step median of all members, zones centre +- 2 / 3 sigma with sigma the pooled
    robust sigma the outlier tests use (log scale: multiplicative, centre * exp(+-k sigma)).
    `threshold_*`: the single-step flag line (spike threshold z) from the tests' own centre and
    pooled sigma (+-POOL_HALF), whatever the drawn window."""

    window: int  # steps the sigma is pooled over (and the centre smoothed over, when > default)
    scale: Scale
    centre: np.ndarray
    lo2: np.ndarray
    hi2: np.ndarray
    lo3: np.ndarray
    hi3: np.ndarray
    sigma: np.ndarray  # analysis units (log or linear)
    threshold_z: float | None
    threshold_lo: np.ndarray | None
    threshold_hi: np.ndarray | None
    outside3: np.ndarray  # per step: unflagged member-steps beyond the drawn 3 sigma
    outside3_members: int  # members with any such step
    cells: int  # unflagged member-steps with a value and a band (the count's denominator)
    widening: np.ndarray  # per step: a p-chart signal (see control_band)
    p_hat: float = P_BEYOND_3  # the window's own share beyond 3 sigma (>= 0.27%)
    phi: float = 1.0  # Pearson overdispersion of the per-step counts (>= 1)
    alpha_step: float = WIDENING_ALPHA  # per-step level (Bonferroni over the steps tested)

    @property
    def outside3_rate(self) -> float:
        return self.outside3_total / self.cells if self.cells else 0.0

    @property
    def outside3_total(self) -> int:
        return int(self.outside3.sum())

    @property
    def expected3(self) -> float:
        """Unflagged member-steps beyond 3 sigma expected if the deviations were normal."""
        return P_BEYOND_3 * self.cells

    @property
    def pool_half(self) -> int:
        return self.window // 2


DEFAULT_BAND_WINDOW = 2 * POOL_HALF + 1


def max_band_window(steps: int | None) -> int:
    """The widest band_window: MAX_BAND_WINDOW steps, and no more than the window has (odd)."""
    cap = MAX_BAND_WINDOW if steps is None else min(steps, MAX_BAND_WINDOW)
    return cap if cap % 2 else cap - 1


def check_band_window(window: int | None, steps: int | None = None) -> int:
    """band_window: None (the tests' pool) or an odd number of steps from 13 to
    min(steps, 121)."""
    if window is None or window == DEFAULT_BAND_WINDOW:
        return DEFAULT_BAND_WINDOW
    cap = max_band_window(steps)
    if window < DEFAULT_BAND_WINDOW or window % 2 == 0 or window > cap:
        limit = (
            f"<= {cap} (at most {MAX_BAND_WINDOW}, and no more than the {steps} steps of the "
            "window)"
            if cap >= DEFAULT_BAND_WINDOW
            else f"is only {DEFAULT_BAND_WINDOW} here (the window has {steps} steps)"
        )
        raise ValueError(
            f"band_window must be an odd number of steps >= {DEFAULT_BAND_WINDOW} and {limit}: "
            f"the outlier tests pool sigma over +-{POOL_HALF} steps, larger is calmer; got {window}"
        )
    return window


_WIDEN: dict[tuple[int, float, float], int] = {}


def widening_count(n: int, p: float = P_BEYOND_3, alpha: float = WIDENING_ALPHA) -> int:
    """The smallest k with P(Bin(n, p) >= k) < alpha (n + 1 when no k reaches it)."""
    key = (n, round(p, 10), round(alpha, 14))
    if key not in _WIDEN:
        k = 1
        while k <= n and binom_sf(k, n, p) >= alpha:
            k += 1
        _WIDEN[key] = k
    return _WIDEN[key]


def widening(count: np.ndarray, n_t: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    """p-chart of the per-step counts of unflagged members beyond 3 sigma: which steps the fleet
    widened faster than the pooled sigma tracks. Returns (marks, p_hat, phi, alpha_step).

    - p_hat = max(0.27%, the window's own share): the reference is this fleet's usual share
      (heavy tails or a MAD-based sigma put more than 0.27% beyond 3 sigma at every step: that is
      the fleet's shape, not widening), never below the normal one.
    - Family-wise over the steps: alpha_step = WIDENING_ALPHA / steps with n_t > 0 (Bonferroni):
      the chance of any mark in the window is <= 1%.
    - Overdispersion: autocorrelated or heavy-tailed members make the counts vary more than
      binomial. Pearson phi = mean over steps of (c - n p)^2 / (n p (1 - p)), floored at 1, and
      the test is quasi-binomial: c / phi against Bin(n / phi, p_hat). Chosen over Laney's p'
      chart, which rescales a normal approximation: at p ~ 0.3% and counts of 0-5 the normal
      tail is far off, while the scaled binomial keeps the exact discrete tail.
    - Each step is judged against p_hat and phi of the OTHER steps (leave-one-out): one widened
      step would otherwise inflate its own phi and hide itself. The returned p_hat and phi are
      the whole window's (stated)."""
    ok = n_t > 0
    marks = np.zeros(count.size, bool)
    if not ok.any():
        return marks, P_BEYOND_3, 1.0, WIDENING_ALPHA
    c, n = count[ok].astype(float), n_t[ok].astype(float)
    k = int(ok.sum())
    alpha = WIDENING_ALPHA / k
    big_c, big_n, big_s = float(c.sum()), float(n.sum()), float(np.sum(c * c / n))

    def ref(cj: float, nj: float, m: int) -> tuple[float, float]:
        """p_hat and Pearson phi of the steps without (cj, nj): sum (c - n p)^2 / (n p (1 - p))
        = (sum c^2 / n - 2 p C + p^2 N) / (p (1 - p))."""
        cc, nn, ss = big_c - cj, big_n - nj, big_s - (cj * cj / nj if nj else 0.0)
        p = min(0.5, max(P_BEYOND_3, cc / nn if nn > 0 else P_BEYOND_3))
        phi = (ss - 2 * p * cc + p * p * nn) / (p * (1 - p)) / max(m, 1)
        return p, max(1.0, phi)

    p_all, phi_all = ref(0.0, 0.0, k)
    if k < 2:
        return marks, p_all, phi_all, alpha
    for j in np.flatnonzero(ok & (count > 0)):
        p, phi = ref(float(count[j]), float(n_t[j]), k - 1)
        ne = max(1, round(int(n_t[j]) / phi))
        marks[j] = count[j] / phi >= widening_count(ne, p, alpha)
    return marks, p_all, phi_all, alpha


def control_band(
    f: Fleet, window: int | None = None, flagged: np.ndarray | None = None
) -> ControlBand:
    """The SPC band of an analysed fleet. window: steps (odd, >= 13): sigma pooled over
    +-window // 2 steps and, above the default, the centre a centred moving median over window
    steps (a calmer band; the flag line stays the tests'). flagged: member-steps the outlier
    tests flagged (whole members for level / change, episode steps for transients); member-steps
    beyond the drawn 3 sigma that are not flagged are counted per step."""
    w = check_band_window(window, f.values.shape[1])
    log = f.scale == "log"
    with np.errstate(divide="ignore", invalid="ignore"):
        y = np.log(f.values) if log else f.values.astype(float)
    c0, s0, _ = _pooled(y)
    if w == DEFAULT_BAND_WINDOW:
        c, s = c0, s0
    else:
        _, s, _ = _pooled(y, w // 2)
        c = rolling_median(c0[None, :], w)[0]
        s = np.where(np.isnan(c), np.nan, s)
    back = np.exp if log else (lambda v: v)
    # the spike bar the tests used: the Gumbel bar replaces the t bar where it is higher
    bars = [
        f.thresholds[k] for k in ("spike_threshold", "spike_tail_threshold") if k in f.thresholds
    ]
    thr = max(bars) if bars else None
    if thr is not None and not math.isfinite(thr):
        thr = None
    with np.errstate(invalid="ignore"):
        dev = np.abs(y - c)
        beyond = dev > 3 * s
    usable = np.isfinite(dev) & np.isfinite(s)[None, :]
    if flagged is not None:
        beyond &= ~flagged
        usable &= ~flagged
    count, n_t = beyond.sum(axis=0), usable.sum(axis=0)
    marks, p_hat, phi, alpha_step = widening(count, n_t)
    return ControlBand(
        window=w, scale=f.scale, centre=back(c), sigma=s,
        lo2=back(c - 2 * s), hi2=back(c + 2 * s), lo3=back(c - 3 * s), hi3=back(c + 3 * s),
        threshold_z=thr,
        threshold_lo=None if thr is None else back(c0 - thr * s0),
        threshold_hi=None if thr is None else back(c0 + thr * s0),
        outside3=count, outside3_members=int(beyond.any(axis=1).sum()),
        cells=int(usable.sum()), widening=marks, p_hat=p_hat, phi=phi, alpha_step=alpha_step,
    )  # fmt: skip


def flagged_steps(shape: tuple[int, int], outliers: list[tuple[int, Outlier]]) -> np.ndarray:
    """Member-steps the tests flagged: every step of a level / change outlier, the episode
    steps of a transient. outliers: (row in the fleet, outlier)."""
    out = np.zeros(shape, bool)
    for i, o in outliers:
        if o.kind == "transient":
            for e in o.episodes:
                out[i, e.start : e.end + 1] = True
        else:
            out[i] = True
    return out


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
    y_raw: np.ndarray,
    scale: str = "auto",
    normalise: Normalise = "none",
    alpha: float = ALPHA,
    unknown: np.ndarray | None = None,
) -> Fleet:
    """Spread, per-step deviations and outlying members of a fleet (members x steps, NaN gaps).
    `unknown`: member-steps whose data cannot be trusted (bucket_state UNKNOWN); their values
    are dropped and they count neither as reporting nor as missing."""
    if unknown is not None and unknown.any():
        y_raw = np.where(unknown, np.nan, y_raw)
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
    sp = spread(band_y, unknown)
    d, z, cnt, loo = loo_deviations(y)
    caveats: list[str] = []
    known = np.ones((m_, t_), bool) if unknown is None else ~unknown
    alive_steps = np.array([int(known[i, f:].sum()) if f >= 0 else 0 for i, f in enumerate(first)])
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
    tests = _level_change_tests(zt, normalise)
    flagged_lc = np.zeros(k, bool)
    for v in tests.values():
        flagged_lc |= np.abs(v) > thr
    tau_hat = typical_tau(d[tested])
    thresholds["tau"] = tau_hat
    scans, bars = _excursion_bars(
        zt, cnt[tested], ~flagged_lc, tau_hat, a_test, thresholds, caveats
    )
    for row, i in enumerate(tested):
        fired, zs, exceed, ratio = _member_tests(row, tests, thr, scans, bars)
        if fired:
            o = _describe(i, z[i], d[i], fired, zs, exceed, ratio, thr, t_, tau_hat)
            if o.kind != "transient":  # both modes: excursions beyond its own level
                resid = z[i] - _own_baseline(z[i], o)
                ex = _excursions(resid, thresholds["phi"], {k_: b[row] for k_, b in bars.items()})
                o.episodes = _episodes(resid, ex, o.tau, beyond_own_level=True)
            outliers.append(o)
    if len(outliers) > MANY_OUTLIERS * k:
        caveats.append("many_outliers")
    outliers.sort(key=lambda o: -o.score)
    return Fleet(sc, normalise, sp, loo, tested, untested, outliers, thresholds, caveats,
                 first, last, z, d, band_y)  # fmt: skip


def _level_change_tests(zt: np.ndarray, normalise: Normalise) -> dict[str, np.ndarray]:
    """Across-member robust z of each tested member's level (trimmed mean z over the window; off
    when members are normalised) and change (last third minus first third)."""
    k, t_ = zt.shape
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
    return tests


def _excursion_bars(
    zt: np.ndarray,
    cnt: np.ndarray,
    calm: np.ndarray,
    tau_hat: float,
    a_test: float,
    thresholds: dict[str, float],
    caveats: list[str],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Per excursion scale: the scanned z (tested members x steps) and the bar each step must
    stay within. `calm`: members no level / change test fired on (the pool for scale and tails).
    Records thresholds and the heavy-tail caveat.

    Excursions at three durations, alpha split: single steps (spikes) and rolling medians over
    5 and 15 steps (episodes: most of the window out, so heavy-tailed single-step noise cannot
    fake them). Light tails: Student t with the pooled sigma's df, Bonferroni over every
    member-step. Heavy tails at a scale (the typical member exceeds t's 0.1% point too often):
    each member's peak also against a Gumbel fitted to the other members' peaks."""
    k = zt.shape[0]
    w = 2 * POOL_HALF + 1
    df = np.maximum(DF_FACTOR * (cnt / min(max(tau_hat, 1.0), w) - 1), 1.0)
    bars: dict[str, np.ndarray] = {}
    # episodes are scanned on AR(1)-prewhitened deviations: under heavy-tailed noise one huge
    # innovation decays over several steps and can hold a rolling median of raw z up (the
    # 15-step scale's false alarms under t(3), lkn.14); innovations do not. A sustained shift
    # delta becomes (1 - phi) delta against innovation noise sd sqrt(1 - phi^2) sigma: for the
    # 15-step median about the same power as raw z at phi = 0.6.
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
            pool = zz[calm]
            ratio = float(np.nanmedian(np.abs(pool))) / float(np.nanmedian(np.abs(zt[calm])))
            unit = np.full(zz.shape, ratio)
        tdf = np.vectorize(lambda v, q=p: t_isf(q / 2, float(round(v, 1))), otypes=[float])(df)
        bar = tdf * unit
        thresholds[f"{name}_threshold"] = float(np.median(bar))
        # a median needing few independent exceedances keeps the single-step tails
        own = heavy_tailed(zz / unit, calm, float(np.median(df)), tau_hat)
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
            gb = gumbel_bars(peaks, calm, a_test / n_scales / k)
            thresholds[f"{name}_tail_threshold"] = (
                float(np.median(gb[np.isfinite(gb)])) if np.isfinite(gb).any() else math.inf
            )
            bar = np.maximum(bar, gb[:, None])
        bars[name] = bar
    return scans, bars


def _member_tests(
    row: int,
    tests: dict[str, np.ndarray],
    thr: float,
    scans: dict[str, np.ndarray],
    bars: dict[str, np.ndarray],
) -> tuple[list[str], dict[str, float], np.ndarray, float]:
    """One tested member (row of the tested matrices): the tests that fired, its z per test,
    the steps beyond an excursion bar and its largest |z| / bar."""
    fired: list[str] = []
    zs: dict[str, float] = {}
    for name, v in tests.items():
        zs[name] = float(v[row])
        if abs(v[row]) > thr:
            fired.append(name)
    exceed = np.zeros(scans["spike"].shape[1], bool)
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
    return fired, zs, exceed, ratio


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


def _episodes(
    zi: np.ndarray, exceed: np.ndarray, tau: float, beyond_own_level: bool = False
) -> list[Episode]:
    """Runs of steps beyond an excursion bar, merged across gaps shorter than tau; sustained
    when longer than tau steps. peak_z: the largest |z| of the run, signed."""
    out = []
    for a, b in _runs(exceed, max(0, math.ceil(tau) - 1)):
        seg = np.where(np.isnan(zi[a : b + 1]), 0.0, zi[a : b + 1])
        j = int(np.argmax(np.abs(seg)))
        out.append(Episode(a, b, float(seg[j]), b - a + 1 > tau, beyond_own_level))
    return out


def _own_baseline(zi: np.ndarray, o: Outlier) -> np.ndarray:
    """A level / change outlier's own expected z per step: its 20% trimmed mean (persistent), the
    trimmed means before and after its split (shifted), its least-squares line (drifting)."""
    t_ = zi.size
    ok = np.isfinite(zi)
    if o.kind == "shifted" and o.at is not None:
        before, after = zi[: o.at], zi[o.at :]
        if np.isfinite(before).sum() >= 3 and np.isfinite(after).sum() >= 3:
            m1, m2 = trimmed_mean(before[None, :])[0], trimmed_mean(after[None, :])[0]
            return np.where(np.arange(t_) < o.at, m1, m2)
    if o.kind in ("drifting", "shifted") and ok.sum() >= 3:
        pos = np.flatnonzero(ok).astype(float)
        coef = np.polyfit(pos, zi[ok], 1)
        return np.polyval(coef, np.arange(t_, dtype=float))
    return np.full(t_, trimmed_mean(zi[None, :])[0])


def _excursions(zi: np.ndarray, phi: float, bars: dict[str, np.ndarray]) -> np.ndarray:
    """Steps where one member's deviation series exceeds the excursion bars (the same scans as
    the excursion tests: single steps, rolling medians of the AR(1) innovations)."""
    ep_in = prewhiten(zi[None, :], phi)
    ex = np.zeros(zi.size, bool)
    for name, w_ in EXCURSIONS.items():
        zz = zi if w_ == 1 else rolling_median(ep_in, w_)[0]
        with np.errstate(invalid="ignore"):
            ex |= np.abs(zz) > bars[name]
    return ex


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
        episodes = _episodes(zi, exceed, tau)
    since, from_start = _since(zi, sign, tau)
    if kind == "transient" and episodes:
        since, from_start = None, False
    return Outlier(
        member=i, kind=kind, direction="higher" if sign > 0 else "lower", score=score,
        fired=fired, z=zs, since=since, since_window_start=from_start, offset=off,
        offset_interval=lo_i, change=chg, change_interval=chg_i, at=at, peak_at=peak_at,
        episodes=episodes, tau=tau, n=int(ok.sum()),
    )  # fmt: skip
