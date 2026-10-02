"""Statistical process control from a stated baseline window (bead lkn.1). Pure numpy.

Limits come ONLY from the baseline; the judged points are everything outside it.
- Centre = baseline median (+ a seasonal curve fitted on the baseline only); sigma = 1.4826 MAD
  of the baseline deviations: the MARGINAL sigma, so the drawn 3-sigma band has its nominal
  0.27% per-point rate under any stationary dependence. (Moving-range sigma, the convention,
  underestimates it under positive autocorrelation and floods the chart.)
- Sequence detectors (rules, EWMA, CUSUM) need independent points: when the baseline lag-1
  phi is significant they run on AR(1) residuals, standardised by their own baseline MAD.
- In-control ARL by Brook-Evans Markov chains; p-values are computed under a conservative
  scenario for the baseline's estimation error (true sigma at the one-sided 95% upper bound,
  centre off by its one-sided 95% bound), so a short baseline does not manufacture alarms.
Design: docs/superpowers/specs/2026-10-02-series-diagnostics-design.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from functools import lru_cache
from statistics import NormalDist

import numpy as np

from telemetry_nerd.analysis.autocorr import ar1, ar1_residuals, n_eff, tau_int
from telemetry_nerd.analysis.fraction import wilson
from telemetry_nerd.analysis.stability import ALPHA, Harmonics, fit_harmonics
from telemetry_nerd.analysis.stats import MAD_VAR, poisson_sf, robust_sigma, z_of

MIN_BASELINE = 30
MIN_BASELINE_EFF = 10
EWMA_LAMBDA, EWMA_L = 0.2, 3.0
CUSUM_K, CUSUM_H = 0.5, 5.0
MEDIAN_SE = 1.2533  # se(median) = 1.2533 sigma / sqrt(n) for a normal
CONSERVATIVE_Z = 1.645  # one-sided 95% for the baseline-estimation scenario
NEAR_RANDOM_WALK = 0.9
_N = NormalDist()

_p2, _p1 = 1 - _N.cdf(2), 1 - _N.cdf(1)
#: per-window false-alarm rate of each rule for iid N(0, 1) with known parameters
RULE_RATES = {
    "beyond_3sigma": 2 * (1 - _N.cdf(3)),
    "2_of_3_beyond_2sigma": 2 * (3 * _p2**2 * (1 - _p2) + _p2**3),
    "4_of_5_beyond_1sigma": 2 * (5 * _p1**4 * (1 - _p1) + _p1**5),
    "8_in_a_row_one_side": 2 * 0.5**8,
}
RULE_WINDOWS = {
    "beyond_3sigma": 1,
    "2_of_3_beyond_2sigma": 3,
    "4_of_5_beyond_1sigma": 5,
    "8_in_a_row_one_side": 8,
}
#: rules whose flagged points are (near) independent: these decide in/out of control
DECIDING = ("beyond_3sigma", "ewma", "cusum")


# detectors ----------------------------------------------------------------------
def run_rules(z: np.ndarray, pos: np.ndarray, judged: np.ndarray) -> dict[str, np.ndarray]:
    """Per rule, a bool per sample: a window of consecutive steps ENDING here, all judged and
    all observed (a gap breaks the window), meets the rule."""
    size = int(pos[-1]) + 1 if pos.size else 0
    dz = np.full(size, np.nan)
    ok = judged & ~np.isnan(z)
    dz[pos[ok]] = z[ok]
    out = {}
    for rule, w in RULE_WINDOWS.items():
        hit = np.zeros(size, bool)
        if size >= w:
            win = np.lib.stride_tricks.sliding_window_view(dz, w)
            full = ~np.isnan(win).any(axis=1)
            wv = np.nan_to_num(win)
            if rule == "beyond_3sigma":
                h = np.abs(wv[:, 0]) > 3
            elif rule == "2_of_3_beyond_2sigma":
                h = ((wv > 2).sum(1) >= 2) | ((wv < -2).sum(1) >= 2)
            elif rule == "4_of_5_beyond_1sigma":
                h = ((wv > 1).sum(1) >= 4) | ((wv < -1).sum(1) >= 4)
            else:
                h = (wv > 0).all(1) | (wv < 0).all(1)
            hit[w - 1 :] = h & full
        out[rule] = hit[pos]
    return out


def windows_available(pos: np.ndarray, ok: np.ndarray, w: int) -> int:
    size = int(pos[-1]) + 1 if pos.size else 0
    if size < w:
        return 0
    d = np.zeros(size, bool)
    d[pos[ok]] = True
    return int(np.lib.stride_tricks.sliding_window_view(d, w).all(1).sum())


def ewma_signals(
    z: np.ndarray,
    lam: float = EWMA_LAMBDA,
    L: float = EWMA_L,
    gaps: np.ndarray | None = None,
) -> list[int]:
    """Indices where |EWMA| crosses the asymptotic limit; the statistic restarts after each.

    `gaps[i]`: steps missing between z[i-1] and z[i]. Across g missing steps the statistic
    decays by (1 - lam)^g, i.e. the time-aware EWMA with the missing points given zero weight
    (never imputed): a long gap is a restart, none is the classic recursion. Its variance only
    shrinks, so the in-control ARL can only grow (conservative)."""
    lim = L * math.sqrt(lam / (2 - lam))
    e, out = 0.0, []
    for i, v in enumerate(z):
        if gaps is not None and gaps[i] > 0:
            e *= (1 - lam) ** int(gaps[i])
        e = (1 - lam) * e + lam * v
        if abs(e) > lim:
            out.append(i)
            e = 0.0
    return out


def cusum_signals(
    z: np.ndarray, k: float = CUSUM_K, h: float = CUSUM_H, gaps: np.ndarray | None = None
) -> list[int]:
    """Two-sided tabular CUSUM; both sides restart after a signal.

    Across g missing steps each side drains by g k (its expected in-control drift per step,
    without imputing values): a gap of h / k steps or more is a restart. Conservative."""
    hi = lo = 0.0
    out = []
    for i, v in enumerate(z):
        if gaps is not None and gaps[i] > 0:
            hi, lo = max(0.0, hi - gaps[i] * k), max(0.0, lo - gaps[i] * k)
        hi, lo = max(0.0, hi + v - k), max(0.0, lo - v - k)
        if hi > h or lo > h:
            out.append(i)
            hi = lo = 0.0
    return out


def _arl(transition: np.ndarray, start: int) -> float:
    m = transition.shape[0]
    arl = np.linalg.solve(np.eye(m) - transition, np.ones(m))
    return float(arl[start])


@lru_cache(maxsize=256)
def ewma_arl(
    lam: float = EWMA_LAMBDA, L: float = EWMA_L, mu: float = 0.0, r: float = 1.0, n: int = 100
) -> float:
    """ARL of the EWMA on z ~ N(mu, r^2): Lucas & Saccucci (1990) Markov chain, 2n+1 states."""
    ucl = L * math.sqrt(lam / (2 - lam))
    m = 2 * n + 1
    w = 2 * ucl / m
    c = -ucl + w * (np.arange(m) + 0.5)
    hi = (c[None, :] + w / 2 - (1 - lam) * c[:, None]) / lam
    lo = (c[None, :] - w / 2 - (1 - lam) * c[:, None]) / lam
    cdf = np.vectorize(_N.cdf)
    p = cdf((hi - mu) / r) - cdf((lo - mu) / r)
    return _arl(p, n)


def _cusum_one_sided(k: float, h: float, mu: float, r: float, n: int) -> float:
    w = h / (n - 0.5)
    v = np.arange(n) * w  # state 0 is exactly 0
    cdf = np.vectorize(_N.cdf)
    p = np.empty((n, n))
    p[:, 0] = cdf((w / 2 - v + k - mu) / r)
    up = (v[None, 1:] + w / 2 - v[:, None] + k - mu) / r
    dn = (v[None, 1:] - w / 2 - v[:, None] + k - mu) / r
    p[:, 1:] = cdf(up) - cdf(dn)
    return _arl(p, 0)


@lru_cache(maxsize=256)
def cusum_arl(
    k: float = CUSUM_K, h: float = CUSUM_H, mu: float = 0.0, r: float = 1.0, n: int = 200
) -> float:
    """Two-sided ARL on z ~ N(mu, r^2): Brook & Evans (1972) chains per side, combined as
    1/ARL = 1/ARL+ + 1/ARL- (Lucas & Crosier; accurate for k > 0)."""
    up, down = _cusum_one_sided(k, h, mu, r, n), _cusum_one_sided(k, h, -mu, r, n)
    return 1 / (1 / up + 1 / down)


def beyond_rate(mu: float = 0.0, r: float = 1.0, limit: float = 3.0) -> float:
    return _N.cdf((-limit - mu) / r) + 1 - _N.cdf((limit - mu) / r)


# chart ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Detector:
    count: int  # exact: flagged points (rules) or signal episodes (ewma/cusum)
    opportunities: int  # windows / judged points the count is out of
    expected: float  # under control, parameters known
    rate_interval: tuple[float, float] | None  # Wilson, 1 - ALPHA (rules only)
    p: float | None  # Poisson tail, conservative scenario; None: overlapping windows
    indices: list[int] = field(default_factory=list)


@dataclass
class ControlChart:
    mode: str  # "individuals" | "ar1_residuals" | "insufficient_data"
    reason: str | None
    baseline: np.ndarray  # bool per sample
    judged: np.ndarray
    centre: np.ndarray  # per sample (seasonal curve + level)
    sigma: float  # marginal
    centre_interval: tuple[float, float] = (0.0, 0.0)  # of the level, 1 - ALPHA
    sigma_interval: tuple[float, float] = (0.0, 0.0)
    n_baseline: int = 0
    n_eff_baseline: float = 0.0
    phi: float = 0.0
    phi_significant: bool = False
    sigma_resid: float | None = None
    outside: Detector | None = None  # raw points outside centre +/- 3 sigma (drawn band)
    detectors: dict[str, Detector] = field(default_factory=dict)
    arl0: dict[str, float] = field(default_factory=dict)
    arl_1sigma: dict[str, float] = field(default_factory=dict)  # points to detect a 1-sigma shift
    seasonal_periods_s: list[float] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    level: float = math.nan  # baseline median of y - seasonal curve
    #: seasonal part of the centre: none | harmonics (fitted on the baseline) | profile (the
    #: operating profile's shape) | harmonics+profile
    seasonal: str = "none"

    def tail(self, k: int) -> ControlChart:
        """The chart restricted to samples k.. (drops a separately fetched baseline that was
        prepended to the judged series); detector indices shift by -k."""
        if k == 0:
            return self

        def det(d: Detector | None) -> Detector | None:
            if d is None:
                return None
            return replace(d, indices=[i - k for i in d.indices if i >= k])

        return replace(
            self,
            baseline=self.baseline[k:],
            judged=self.judged[k:],
            centre=self.centre[k:],
            outside=det(self.outside),
            detectors={n: det(d) for n, d in self.detectors.items()},  # type: ignore[misc]
        )

    @property
    def in_control(self) -> bool | None:
        if self.mode == "insufficient_data":
            return None
        return all((d.p is None or d.p >= ALPHA / len(DECIDING)) for d in self.detectors.values())

    def violations(self) -> dict[int, list[str]]:
        """Sample index -> rules it violated (judged points only)."""
        out: dict[int, list[str]] = {}
        if self.outside:
            for i in self.outside.indices:
                out.setdefault(i, []).append("outside_limits")
        for name, d in self.detectors.items():
            for i in d.indices:
                out.setdefault(i, []).append(name)
        return dict(sorted(out.items()))


def covered_by(period_s: float, cycle_s: float) -> bool:
    """A period the profile's cycle models: the cycle or one of its harmonics, >= 2 h (the
    profile has hourly buckets)."""
    if period_s < 2 * 3600:
        return False
    r = cycle_s / period_s
    return abs(r - round(r)) <= 0.05 * r


def _insufficient(baseline, judged, y, reason, caveats=()) -> ControlChart:
    return ControlChart(
        "insufficient_data", reason, baseline, judged, np.full(y.size, np.nan), math.nan,
        caveats=list(caveats),
    )  # fmt: skip


def control_chart(
    pos: np.ndarray,
    t_s: np.ndarray,
    y: np.ndarray,
    baseline: np.ndarray,
    harmonics: Harmonics | None = None,
    profile: np.ndarray | None = None,
    profile_cycle_s: float | None = None,
) -> ControlChart:
    """`baseline`: bool per sample. `harmonics`: periods to model, re-fitted on the baseline.

    `profile`: a seasonal shape per sample from the operating profile (estimated WITHOUT the
    judged data), whose cycle is `profile_cycle_s`. It is the seasonal centre whenever the
    baseline cannot fit that cycle itself (fewer than 2 cycles): the chart is then a seasonal
    residual chart (y - shape), with level and sigma still from the baseline only."""
    judged = ~baseline
    nb = int(baseline.sum())
    if nb < MIN_BASELINE:
        return _insufficient(
            baseline, judged, y, f"baseline has {nb} points, needs >= {MIN_BASELINE}"
        )
    caveats: list[str] = []
    periods = list(harmonics.periods_s) if harmonics else []
    # how much of a cycle the baseline can fit: its span, but no more than the time it
    # actually observed (several short same-phase windows span days and cover a few hours)
    tb = t_s[baseline]
    step_s = float(np.median(np.diff(t_s))) if t_s.size > 1 else 0.0
    cover_b = min(float(tb.max() - tb.min()) + step_s, nb * step_s)
    if profile is not None and (not profile_cycle_s or cover_b >= 2 * profile_cycle_s):
        profile = None  # the baseline holds 2 cycles: it fits the cycle itself (harmonics)
    if periods:
        kept = [p for p in periods if cover_b >= 2 * p]
        dropped = [p for p in periods if p not in kept]
        if profile is not None and profile_cycle_s:
            dropped = [p for p in dropped if not covered_by(p, profile_cycle_s)]
        if dropped:
            caveats.append("seasonal_not_in_baseline")
        periods = kept
    shape_ = np.zeros(y.size) if profile is None else np.asarray(profile, float)
    if np.isnan(shape_).any():
        shape_ = np.nan_to_num(shape_)  # unmodelled phases: no seasonal adjustment there
    seas = fit_harmonics(t_s[baseline], y[baseline] - shape_[baseline], periods)
    curve = seas.curve(t_s) + shape_
    seasonal = (
        "+".join(
            name
            for name, on in (("harmonics", bool(periods)), ("profile", profile is not None))
            if on
        )
        or "none"
    )
    yb = y[baseline] - curve[baseline]
    level = float(np.median(yb))
    sigma = robust_sigma(yb)
    if sigma == 0:
        return _insufficient(baseline, judged, y, "baseline is constant (sigma = 0)", caveats)
    d = y - curve - level
    tau = tau_int(pos[baseline], d[baseline])
    ne = n_eff(nb, tau)
    if ne < MIN_BASELINE_EFF:
        return _insufficient(
            baseline, judged, y,
            f"baseline n_eff {ne:.1f} < {MIN_BASELINE_EFF} (autocorrelation time {tau:.1f} steps)",
            caveats,
        )  # fmt: skip
    za = z_of(1 - ALPHA / 2)
    c_se = MEDIAN_SE * sigma / math.sqrt(ne)
    s_se = math.sqrt(MAD_VAR / ne)
    fit = ar1(pos[baseline], d[baseline])
    chart = ControlChart(
        "individuals", None, baseline, judged, curve + level, sigma,
        (level - za * c_se, level + za * c_se),
        (sigma * math.exp(-za * s_se), sigma * math.exp(za * s_se)),
        nb, ne, fit.phi, fit.significant, seasonal_periods_s=periods, caveats=caveats,
        level=level, seasonal=seasonal,
    )  # fmt: skip
    if ne < 100:
        caveats.append("short_baseline")
    # conservative scenario for the baseline's estimation error, in units of the detector's z
    r_c = math.exp(CONSERVATIVE_Z * s_se)
    mu_c = CONSERVATIVE_Z * c_se / sigma
    z = d / sigma
    zmask = np.ones(y.size, bool)
    if fit.significant:
        chart.mode = "ar1_residuals"
        ok, e = ar1_residuals(pos, d, fit.phi)
        eb = e[baseline[ok]]
        se_ = robust_sigma(eb)
        if se_ == 0:
            return _insufficient(baseline, judged, y, "baseline residuals are constant", caveats)
        chart.sigma_resid = se_
        z = np.full(y.size, np.nan)
        z[ok] = e / se_
        zmask = ok
        s_se_r = math.sqrt(MAD_VAR / max(int(ok[baseline].sum()), 1))
        r_c = math.exp(CONSERVATIVE_Z * s_se_r)
        mu_c = (1 - fit.phi) * CONSERVATIVE_Z * c_se / se_
        if fit.phi > NEAR_RANDOM_WALK:
            caveats.append("near_random_walk")
    # drawn band on the raw series: marginal sigma
    out_mask = judged & (np.abs(d) > 3 * sigma)
    nj = int(judged.sum())
    nj_eff = n_eff(nj, tau)
    rate_raw = out_mask.sum() / max(nj, 1)
    chart.outside = Detector(
        int(out_mask.sum()), nj, nj * RULE_RATES["beyond_3sigma"],
        wilson(rate_raw * nj_eff, nj_eff, za) if nj else None, None,
        [int(i) for i in np.flatnonzero(out_mask)],
    )  # fmt: skip
    rules = run_rules(z, pos, judged)
    jz = judged & zmask
    for name, hit in rules.items():
        w = RULE_WINDOWS[name]
        opp = windows_available(pos, jz, w)
        cnt = int(hit.sum())
        p = None
        if name == "beyond_3sigma":
            p = poisson_sf(cnt, opp * beyond_rate(mu_c, r_c))
        chart.detectors[name] = Detector(
            cnt, opp, opp * RULE_RATES[name],
            wilson(cnt, opp, za) if opp else None, p,
            [int(i) for i in np.flatnonzero(hit)],
        )  # fmt: skip
    idx = np.flatnonzero(jz)
    zs = z[idx]
    # steps missing before each judged z (gaps, or a baseline stretch between judged parts)
    gaps = np.r_[0, np.diff(pos[idx]) - 1] if idx.size else np.zeros(0, np.int64)
    chart.arl0 = {"ewma": ewma_arl(), "cusum": cusum_arl()}
    chart.arl_1sigma = {"ewma": ewma_arl(mu=1.0), "cusum": cusum_arl(mu=1.0)}
    for name, sig, arl_c in (
        ("ewma", ewma_signals(zs, gaps=gaps), ewma_arl(mu=mu_c, r=r_c)),
        ("cusum", cusum_signals(zs, gaps=gaps), cusum_arl(mu=mu_c, r=r_c)),
    ):
        chart.detectors[name] = Detector(
            len(sig), int(idx.size), idx.size / chart.arl0[name], None,
            poisson_sf(len(sig), idx.size / arl_c), [int(idx[i]) for i in sig],
        )  # fmt: skip
    return chart
