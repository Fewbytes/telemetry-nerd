"""One-call series diagnostics: periodicity, stability, SPC, shape -> verdict (bead lkn.1).

Pure: takes one series (ts on a step grid, gaps allowed) and a baseline mask. Composition only;
the statistics live in spectrum / autocorr / stability / spc.
Design: docs/superpowers/specs/2026-10-02-series-diagnostics-design.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np

from telemetry_nerd.analysis.autocorr import n_eff, positions, tau_int
from telemetry_nerd.analysis.spc import ControlChart, control_chart
from telemetry_nerd.analysis.spectrum import MIN_POINTS, WINDOW_ARTIFACT, Peak, Spectrum
from telemetry_nerd.analysis.stability import (
    KPSS,
    Harmonics,
    Period,
    Shape,
    Shift,
    Trend,
    VarianceRatio,
    changepoints,
    confirm_periods,
    fit_harmonics,
    kpss,
    shape,
    trend,
    variance_ratio,
)
from telemetry_nerd.analysis.stats import robust_sigma

MATERIAL = 0.25  # below this (in sigma) a significant shift / change is only noted as minor
SMALL = 1.0  # below this it is called small
MIN_N_EFF = 10
MAX_PERIODS = 3
VARIANCE_MATERIAL = 1.5
VERDICTS = ("insufficient_data", "level_shifted", "drifting", "periodic", "noisy", "stable")


@dataclass
class Diagnosis:
    verdict: str
    also: list[str]
    reasons: list[str]
    n: int
    tau: float
    n_eff: float
    peaks: list[Peak] = field(default_factory=list)  # confirmed periods (spectrum peaks)
    periods: list[Period] = field(default_factory=list)  # their red-noise test
    candidates: list[Peak] = field(default_factory=list)  # white-noise significant
    harmonics: Harmonics | None = None
    trend: Trend | None = None
    shifts: list[Shift] = field(default_factory=list)
    sigma_within: float = math.nan
    kpss: KPSS | None = None
    variance: VarianceRatio | None = None
    shape: Shape | None = None
    chart: ControlChart | None = None
    model: str = "none"  # which structure explains the series best: none | trend | step
    caveats: list[str] = field(default_factory=list)


def _bic(sse: float, n: int, k: int) -> float:
    return n * math.log(max(sse, 1e-300) / n) + k * math.log(n)


def candidate_peaks(sp: Spectrum | None) -> list[Peak]:
    """Peaks significant against white noise and not sampling artefacts, strongest first."""
    if sp is None:
        return []
    good = [p for p in sp.peaks if p.white_significant and p.window <= WINDOW_ARTIFACT]
    return sorted(good, key=lambda p: -p.power)


@dataclass(frozen=True)
class Structure:
    model: str  # none | trend | step
    trend: Trend
    shifts: list[Shift]
    fitted: np.ndarray
    resid: np.ndarray


def structure(pos, ts_ms, t_s, y, span_ms) -> Structure:
    """Best of constant / linear trend / significant level shifts, by BIC."""
    n = y.size
    tr = trend(pos, ts_ms, y, span_ms)
    shifts = changepoints(pos, ts_ms, y)
    fits = {"none": np.full(n, y.mean())}
    t = t_s - t_s.mean()
    fits["trend"] = y.mean() + (t @ (y - y.mean())) / (t @ t) * t
    if shifts:
        bounds = [0, *[s.index for s in shifts], n]
        fits["step"] = np.concatenate([np.full(b - a, y[a:b].mean()) for a, b in pairwise(bounds)])  # fmt: skip
    k = {"none": 1, "trend": 2, "step": 2 * len(shifts) + 1}
    bics = {m: _bic(float(np.sum((y - f) ** 2)), n, k[m]) for m, f in fits.items()}
    model = min(bics, key=bics.__getitem__)
    return Structure(model, tr, shifts, fits[model], y - fits[model])


def _chart(ts_ms, y, step_ms, pos, t_s, baseline, harm, reference, profile) -> ControlChart:
    shape, cycle = profile if profile is not None else (None, None)
    if reference is None:
        return control_chart(pos, t_s, y, baseline, harm, shape, cycle)
    rts, ry = np.asarray(reference[0], np.int64), np.asarray(reference[1], float)
    k = rts.size
    if k == 0:
        return _no_reference(y)
    ts_all = np.r_[rts, ts_ms]
    if np.any(np.diff(ts_all) <= 0):
        raise ValueError("the reference baseline must lie before the series")
    base = np.r_[np.ones(k, bool), np.zeros(y.size, bool)]
    chart = control_chart(
        positions(ts_all, step_ms), (ts_all - ts_all[0]) / 1000.0, np.r_[ry, y], base, harm,
        shape, cycle,
    )  # fmt: skip
    return chart.tail(k)


def _no_reference(y) -> ControlChart:
    return ControlChart(
        "insufficient_data", "no data in the reference baseline", np.zeros(y.size, bool),
        np.ones(y.size, bool), np.full(y.size, np.nan), math.nan,
    )  # fmt: skip


def diagnose(
    ts_ms: np.ndarray,
    y: np.ndarray,
    step_ms: int,
    baseline: np.ndarray,
    sp: Spectrum | None,
    fmt=lambda v: f"{v:.3g}",
    fmt_ts=str,
    reference: tuple[np.ndarray, np.ndarray] | None = None,
    profile: tuple[np.ndarray, float] | None = None,
) -> Diagnosis:
    """`baseline`: bool per sample (SPC limits come only from these). `sp`: the series'
    spectrum, or None when the range is too short to resolve any period.

    `reference` (ts_ms, y): a separately fetched baseline (an earlier window, e.g. the same
    hours last week); when given, SPC limits come only from it and every point of the series
    is judged (`baseline` is ignored). `profile` (shape, cycle_s): the operating profile's
    seasonal shape per sample of [reference..., series...], estimated without the series.

    Periods and structure confound each other (a step has 1/f^2 power; a slow cycle looks like
    a shift), so: structure on the raw series -> periods confirmed against red noise on what
    the structure leaves -> structure again on the de-seasonalised series."""
    y = np.asarray(y, float)
    pos = positions(ts_ms, step_ms)
    t_s = (ts_ms - ts_ms[0]) / 1000.0
    n = int(y.size)
    span_ms = int(ts_ms[-1] - ts_ms[0]) + step_ms
    caveats: list[str] = []
    if n < MIN_POINTS:
        return Diagnosis(
            "insufficient_data", [], [f"{n} points < {MIN_POINTS}"], n, math.nan, math.nan
        )
    tau_raw = tau_int(pos, y)
    cands = candidate_peaks(sp)
    first = structure(pos, ts_ms, t_s, y, span_ms)
    n_freqs = max(1.0, n / 2)
    confirmed = confirm_periods(
        pos, t_s, first.resid, [p.period_ms / 1000 for p in cands], step_ms / 1000, n_freqs
    )[:MAX_PERIODS]
    periods = [c.period_s for c in confirmed]
    peaks = [p for p in cands if p.period_ms / 1000 in periods]
    harm = fit_harmonics(t_s, first.resid, periods)
    yd = y - harm.curve(t_s)
    st_ = structure(pos, ts_ms, t_s, yd, span_ms) if periods else first
    tr, shifts, model, resid = st_.trend, st_.shifts, st_.model, st_.resid
    sigma_within = robust_sigma(resid) or float(np.std(resid))
    tau = tau_int(pos, resid)
    ne = n_eff(n, tau)
    st = kpss(yd)
    vr = variance_ratio(pos, resid)
    sh = shape(y, tau_raw)
    chart = _chart(ts_ms, y, step_ms, pos, t_s, baseline, harm if peaks else None, reference, profile)  # fmt: skip
    if sp is not None:
        caveats += [c for c in sp.caveats if c not in caveats]
    caveats += [c for c in chart.caveats if c not in caveats]

    labels: list[str] = []
    reasons: list[str] = []
    if ne < MIN_N_EFF:
        labels.append("insufficient_data")
        reasons.append(
            f"n_eff {ne:.1f} < {MIN_N_EFF}: {n} points but autocorrelation time {tau:.1f} steps"
        )
    material = [s for s in shifts if abs(s.delta) >= MATERIAL * sigma_within]
    if material and model == "step":
        labels.append("level_shifted")
        for s in material:
            reasons.append(
                f"level shift {fmt(s.delta)} [{fmt(s.interval[0])}, {fmt(s.interval[1])}] at "
                f"{fmt_ts(s.ts_ms)} ({abs(s.delta) / sigma_within:.1f} sigma"
                f"{', small' if abs(s.delta) < SMALL * sigma_within else ''}, p={s.p:.1g})"
            )
    for s in shifts:
        if s not in material:
            reasons.append(
                f"minor shift {fmt(s.delta)} at {fmt_ts(s.ts_ms)} (p={s.p:.1g}, "
                f"{abs(s.delta) / sigma_within:.1f} sigma < {MATERIAL:g})"
            )
    drift_material = abs(tr.change) >= MATERIAL * tr.sigma_resid
    if tr.significant and drift_material and model == "trend":
        labels.append("drifting")
        reasons.append(
            f"trend {fmt(tr.change)} over the range [{fmt(tr.change_interval[0])}, "
            f"{fmt(tr.change_interval[1])}] ({fmt(tr.slope_per_h)}/h, 99%, n_eff {tr.n_eff:.0f})"
        )
    elif tr.significant and model == "trend":
        reasons.append(f"minor trend {fmt(tr.change)} over the range (< {MATERIAL:g} sigma)")
    if peaks:
        labels.append("periodic")
        reasons.append(
            "periods "
            + ", ".join(f"{c.period_s:.4g}s (fap {c.fap:.1g} vs red noise)" for c in confirmed)
            + f"; amplitude {', '.join(fmt(a) for a in harm.amplitudes())}"
        )
    # a (even minor) shift or trend explains an out-of-control chart: that is not noise
    structured = bool({"level_shifted", "drifting"} & set(labels)) or (
        (model == "step" and bool(shifts)) or (model == "trend" and tr.significant)
    )
    noisy = []
    if vr is not None and vr.significant and not 1 / VARIANCE_MATERIAL < vr.ratio < VARIANCE_MATERIAL:  # fmt: skip
        noisy.append(
            f"variance changes: last/first third scale {vr.ratio:.2g} "
            f"[{vr.interval[0]:.2g}, {vr.interval[1]:.2g}]"
        )
    if "heavy_tails" in sh.flags:
        noisy.append(
            f"heavy tails: excess kurtosis {sh.excess_kurtosis:.2g} "
            f"[{sh.kurtosis_interval[0]:.2g}, {sh.kurtosis_interval[1]:.2g}]"
        )
    if st.p_upper <= 0.01 and not structured:
        noisy.append(
            f"not level-stationary (KPSS {st.stat:.2g}, p <= 0.01) but no single trend or "
            "shift explains it: wandering / red noise"
        )
    if chart.in_control is False and not structured:
        noisy.append("out of control against the baseline without a sustained shift")
    if noisy:
        labels.append("noisy")
        reasons += noisy
    if not labels:
        labels.append("stable")
        reasons.append(
            "no trend, shift or period; in control against the baseline"
            if chart.in_control
            else "no trend, shift or period"
        )
    if "bimodal" in sh.flags and "level_shifted" in labels:
        caveats.append("bimodal_from_shift")
    if chart.mode == "insufficient_data" and chart.reason:
        reasons.append(f"SPC: {chart.reason}")
    order = sorted(labels, key=VERDICTS.index)
    return Diagnosis(
        order[0], order[1:], reasons, n, tau, ne, peaks, confirmed, cands, harm if peaks else None, tr, shifts,
        sigma_within, st, vr, sh, chart, model, caveats,
    )  # fmt: skip
