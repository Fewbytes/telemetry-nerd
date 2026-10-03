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
from telemetry_nerd.analysis.sources import COMMON, SPECIAL, UNDETERMINED, item
from telemetry_nerd.analysis.spc import DECIDING, ControlChart, control_chart
from telemetry_nerd.analysis.spectrum import MIN_POINTS, WINDOW_ARTIFACT, Peak, Spectrum
from telemetry_nerd.analysis.stability import ALPHA as SPC_ALPHA
from telemetry_nerd.analysis.stability import (
    KPSS,
    MIN_SEGMENT,
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
from telemetry_nerd.analysis.stats import poisson_interval, robust_sigma

MATERIAL = 0.25  # below this (in sigma) a significant shift / change is only noted as minor
SMALL = 1.0  # below this it is called small
MIN_N_EFF = 10
#: fewest points diagnose judges: one changepoint test (two segments of MIN_SEGMENT). Periods
#: need the spectrum's MIN_POINTS; a shorter series is judged without a period search (e.g.
#: 30 min at 1 m = 31 points, or a counter series born mid-range read as 0 before birth, 7thi)
MIN_DIAGNOSE_POINTS = 2 * MIN_SEGMENT
MAX_PERIODS = 3
VARIANCE_MATERIAL = 1.5
SIGNAL_P = SPC_ALPHA / len(DECIDING)  # a detector's count is significant below this (in_control)
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
    #: labelled findings (spec §5.4): {source, finding}; measurement-system items are added
    #: from the caveats by the op
    variation: list[dict] = field(default_factory=list)
    departure: Departure | None = None  # events after an all-zero baseline (event counts only)

    @property
    def shifted_from(self) -> int | None:
        """Index of the first material level shift (the step model): signals after it belong to
        that special cause."""
        if self.model != "step" or "level_shifted" not in (self.verdict, *self.also):
            return None
        material = [s.index for s in self.shifts if abs(s.delta) >= MATERIAL * self.sigma_within]
        return min(material) if material else None


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
    seasonal, cycle = profile if profile is not None else (None, None)
    if reference is None:
        return control_chart(pos, t_s, y, baseline, harm, seasonal, cycle)
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
        seasonal, cycle,
    )  # fmt: skip
    return chart.tail(k)


def detector_source(chart: ControlChart, name: str) -> str | None:
    """Source of a detector's signals (spec §5.4). A deciding detector (independent points,
    Poisson-tested) is special cause when its count is significant, else its signals are the
    false alarms common cause produces. The other rules (overlapping windows, no calibrated
    test) follow the chart: common cause in control; out of control they cannot be told apart
    from the special cause on their own (undetermined)."""
    det = chart.outside if name == "outside_limits" else chart.detectors.get(name)
    if det is None or det.count == 0 or chart.in_control is None:
        return None
    if name in DECIDING and det.p is not None:
        return SPECIAL if det.p < SIGNAL_P else COMMON
    return COMMON if chart.in_control else UNDETERMINED


def violation_sources(chart: ControlChart, shifted_from: int | None = None) -> dict[int, str]:
    """Sample index -> source of its SPC signals: special cause when one of its rules is a
    significant deciding detector, or when it lies after a material level shift on an
    out-of-control chart (the shift is the assignable cause); else the weakest label among its
    rules."""
    srcs = {n: detector_source(chart, n) for n in ("outside_limits", *chart.detectors)}
    after = shifted_from if chart.in_control is False else None
    out = {}
    for i, rules in chart.violations().items():
        got = {srcs.get(r) for r in rules}
        if SPECIAL in got or (after is not None and i >= after):
            out[i] = SPECIAL
        else:
            out[i] = UNDETERMINED if UNDETERMINED in got else COMMON
    return out


def _chart_variation(chart: ControlChart, shifted_from: int | None) -> list[dict]:
    if chart.mode == "insufficient_data":
        return []
    out = [item(COMMON, "control limits (baseline centre +- 3 sigma): the common-cause envelope; "
                "points inside it are not to be chased")]  # fmt: skip
    vs = list(violation_sources(chart, shifted_from).values())
    why = {
        COMMON: "no more than the false alarms common cause produces (chart in control)",
        SPECIAL: "beyond what common cause explains (a significant detector, or after the level "
        "shift): investigate",
        UNDETERMINED: "run rules on an out-of-control chart without a significant detector of "
        "their own",
    }
    for src in (SPECIAL, UNDETERMINED, COMMON):
        if k := vs.count(src):
            out.append(item(src, f"{k} point(s) with SPC signals: {why[src]}", points=k))
    return out


def _no_reference(y) -> ControlChart:
    return ControlChart(
        "insufficient_data", "no data in the reference baseline", np.zeros(y.size, bool),
        np.ones(y.size, bool), np.full(y.size, np.nan), math.nan,
    )  # fmt: skip


@dataclass(frozen=True)
class Departure:
    """Events after a baseline that saw none (an event-count series, e.g. an error counter
    born on its first event and read as 0 before it): the SPC chart has no common-cause
    envelope to judge against, and a burst shorter than two changepoint segments escapes the
    changepoint tests. Exact conditional test of one Poisson rate across baseline and judged
    steps: given the A events seen, P(none in the n_b baseline steps) = (n_j / (n_b + n_j))^A.
    Dispersion and autocorrelation come from the baseline, as in the binding verdicts; an
    all-zero baseline has none to estimate, so the counts are taken as Poisson (clustered
    events, e.g. retries of one request, make p optimistic: the method says so)."""

    index: int  # first judged sample with events (descriptive: the test has no split search)
    ts_ms: int
    n_baseline: int
    n_judged: int
    events: int  # judged events, whole (floor: conservative)
    mean: float  # judged mean, the series' units
    interval: tuple[float, float]  # exact Poisson (Garwood) 1 - ALPHA, the series' units
    p: float

    @property
    def significant(self) -> bool:
        return self.p < SPC_ALPHA


DEPARTURE_METHOD = (
    "events after an all-zero baseline: exact conditional test of one Poisson rate across "
    "baseline and judged steps, p = (n_judged / n)^events; dispersion and autocorrelation from "
    "the baseline (none to estimate in an all-zero one: Poisson; clustered events make p "
    "optimistic); judged mean with its exact Poisson (Garwood) 99% interval"
)


def departure_from_zero(
    ts_ms: np.ndarray,
    y: np.ndarray,
    baseline: np.ndarray,
    events_scale: float,
    reference: np.ndarray | None = None,
) -> Departure | None:
    """`events_scale`: events per step = value x events_scale. `baseline`: bool per sample;
    `reference`: values of a separately fetched baseline (then every sample is judged). None
    unless the baseline holds >= MIN_SEGMENT points, all exactly 0, and the judged steps an
    event."""
    y = np.asarray(y, float)
    if reference is not None:
        base, judged, idx = np.asarray(reference, float), y, np.arange(y.size)
    else:
        base, judged, idx = y[baseline], y[~baseline], np.flatnonzero(~baseline)
    if base.size < MIN_SEGMENT or judged.size == 0 or np.any(base != 0) or events_scale <= 0:
        return None
    events = math.floor(float(judged.sum()) * events_scale + 1e-9)
    if events < 1:
        return None
    nb, nj = int(base.size), int(judged.size)
    p = math.exp(events * math.log(nj / (nb + nj)))
    lo, hi = poisson_interval(events, SPC_ALPHA)
    unit = nj * events_scale
    first = int(idx[int(np.flatnonzero(judged > 0)[0])])
    return Departure(
        first, int(ts_ms[first]), nb, nj, events, float(judged.mean()), (lo / unit, hi / unit), p
    )


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
    events_scale: float | None = None,
) -> Diagnosis:
    """`baseline`: bool per sample (SPC limits come only from these). `sp`: the series'
    spectrum, or None when the range is too short to resolve any period.

    `reference` (ts_ms, y): a separately fetched baseline (an earlier window, e.g. the same
    hours last week); when given, SPC limits come only from it and every point of the series
    is judged (`baseline` is ignored). `profile` (shape, cycle_s): the operating profile's
    seasonal shape per sample of [reference..., series...], estimated without the series.
    `events_scale`: the series counts events (value x events_scale = events per step, e.g.
    increase() of a counter); then a baseline that saw none is tested for a departure from
    zero (`Departure`).

    Periods and structure confound each other (a step has 1/f^2 power; a slow cycle looks like
    a shift), so: structure on the raw series -> periods confirmed against red noise on what
    the structure leaves -> structure again on the de-seasonalised series."""
    y = np.asarray(y, float)
    pos = positions(ts_ms, step_ms)
    t_s = (ts_ms - ts_ms[0]) / 1000.0
    n = int(y.size)
    span_ms = int(ts_ms[-1] - ts_ms[0]) + step_ms
    caveats: list[str] = []
    if n < MIN_DIAGNOSE_POINTS:
        return Diagnosis(
            "insufficient_data", [], [f"{n} points < {MIN_DIAGNOSE_POINTS}"], n, math.nan,
            math.nan,
        )  # fmt: skip
    if n < MIN_POINTS:
        sp = None  # too short to resolve a period against red noise: none is searched
        caveats.append("no_period_search")
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
    final = structure(pos, ts_ms, t_s, yd, span_ms) if periods else first
    tr, shifts, model, resid = final.trend, final.shifts, final.model, final.resid
    sigma_within = robust_sigma(resid) or float(np.std(resid))
    tau = tau_int(pos, resid)
    ne = n_eff(n, tau)
    stationarity = kpss(yd)
    vr = variance_ratio(pos, resid)
    sh = shape(y, tau_raw)
    chart = _chart(ts_ms, y, step_ms, pos, t_s, baseline, harm if peaks else None, reference, profile)  # fmt: skip
    if sp is not None:
        caveats += [c for c in sp.caveats if c not in caveats]
    caveats += [c for c in chart.caveats if c not in caveats]

    labels: list[str] = []
    reasons: list[str] = []
    var: list[dict] = []
    dep = (
        departure_from_zero(
            ts_ms, y, baseline, events_scale, None if reference is None else reference[1]
        )
        if events_scale
        else None
    )
    departed = dep is not None and dep.significant
    if ne < MIN_N_EFF:
        reasons.append(
            f"n_eff {ne:.1f} < {MIN_N_EFF}: {n} points but autocorrelation time {tau:.1f} steps"
            + (": trend, shift and period tests are unreliable; the departure from the zero "
               "baseline rests on the event counts alone" if departed else "")
        )  # fmt: skip
        if not departed:
            labels.append("insufficient_data")
    material = [s for s in shifts if abs(s.delta) >= MATERIAL * sigma_within]
    if material and model == "step":
        labels.append("level_shifted")
        for s in material:
            reasons.append(
                f"level shift {fmt(s.delta)} [{fmt(s.interval[0])}, {fmt(s.interval[1])}] at "
                f"{fmt_ts(s.ts_ms)} ({abs(s.delta) / sigma_within:.1f} sigma"
                f"{', small' if abs(s.delta) < SMALL * sigma_within else ''}, p={s.p:.1g})"
            )
            var.append(item(SPECIAL, reasons[-1]))
    for s in shifts:
        if s not in material:
            reasons.append(
                f"minor shift {fmt(s.delta)} at {fmt_ts(s.ts_ms)} (p={s.p:.1g}, "
                f"{abs(s.delta) / sigma_within:.1f} sigma < {MATERIAL:g})"
            )
            var.append(item(SPECIAL, reasons[-1]))
    drift_material = abs(tr.change) >= MATERIAL * tr.sigma_resid
    if tr.significant and drift_material and model == "trend":
        labels.append("drifting")
        reasons.append(
            f"trend {fmt(tr.change)} over the range [{fmt(tr.change_interval[0])}, "
            f"{fmt(tr.change_interval[1])}] ({fmt(tr.slope_per_h)}/h, 99%, n_eff {tr.n_eff:.0f})"
        )
        var.append(item(SPECIAL, reasons[-1]))
    elif tr.significant and model == "trend":
        reasons.append(f"minor trend {fmt(tr.change)} over the range (< {MATERIAL:g} sigma)")
        var.append(item(SPECIAL, reasons[-1]))
    if dep is not None:
        what = (
            f"{dep.events} events in {dep.n_judged} judged steps after {dep.n_baseline} baseline "
            f"steps with none (first at {fmt_ts(dep.ts_ms)}; mean {fmt(dep.mean)} "
            f"[{fmt(dep.interval[0])}, {fmt(dep.interval[1])}] per step, Poisson p={dep.p:.1g})"
        )
        if departed:
            if "level_shifted" not in labels:
                labels.append("level_shifted")
            reasons.append(f"departure from a zero baseline: {what}")
            var.append(item(SPECIAL, reasons[-1]))
        else:
            reasons.append(f"events after a zero baseline, within what one rate explains: {what}")
            var.append(item(COMMON, reasons[-1]))
    if peaks:
        labels.append("periodic")
        reasons.append(
            "periods "
            + ", ".join(f"{c.period_s:.4g}s (fap {c.fap:.1g} vs red noise)" for c in confirmed)
            + f"; amplitude {', '.join(fmt(a) for a in harm.amplitudes())}"
        )
        var.append(item(COMMON, reasons[-1] + ": a systemic cycle, part of the envelope"))
    # a (even minor) shift or trend explains an out-of-control chart: that is not noise
    structured = (
        departed
        or bool({"level_shifted", "drifting"} & set(labels))
        or ((model == "step" and bool(shifts)) or (model == "trend" and tr.significant))
    )
    noisy: list[tuple[str, str]] = []
    if vr is not None and vr.significant and not 1 / VARIANCE_MATERIAL < vr.ratio < VARIANCE_MATERIAL:  # fmt: skip
        noisy.append((SPECIAL, (
            f"variance changes: last/first third scale {vr.ratio:.2g} "
            f"[{vr.interval[0]:.2g}, {vr.interval[1]:.2g}]"
        )))  # fmt: skip
    if "heavy_tails" in sh.flags:
        noisy.append((COMMON, (
            f"heavy tails: excess kurtosis {sh.excess_kurtosis:.2g} "
            f"[{sh.kurtosis_interval[0]:.2g}, {sh.kurtosis_interval[1]:.2g}]"
        )))  # fmt: skip
    if stationarity.p_upper <= 0.01 and not structured:
        noisy.append((COMMON, (
            f"not level-stationary (KPSS {stationarity.stat:.2g}, p <= 0.01) but no single trend or "
            "shift explains it: wandering / red noise"
        )))  # fmt: skip
    if chart.in_control is False and not structured:
        noisy.append((SPECIAL, "out of control against the baseline without a sustained shift"))
    if noisy:
        labels.append("noisy")
        reasons += [r for _, r in noisy]
        var += [item(src, r) for src, r in noisy]
    if not labels:
        labels.append("stable")
        reasons.append(
            "no trend, shift or period; in control against the baseline"
            if chart.in_control
            else "no trend, shift or period"
        )
        var.append(item(COMMON, reasons[-1]))
    shifted = "level_shifted" in labels and model == "step" and bool(material)
    var += _chart_variation(
        chart,
        min(s.index for s in material) if shifted else (dep.index if departed and dep else None),
    )
    if "bimodal" in sh.flags and "level_shifted" in labels:
        caveats.append("bimodal_from_shift")
    if chart.mode == "insufficient_data" and chart.reason:
        reasons.append(f"SPC: {chart.reason}")
    order = sorted(labels, key=VERDICTS.index)
    return Diagnosis(
        order[0], order[1:], reasons, n, tau, ne, peaks, confirmed, cands, harm if peaks else None, tr, shifts,
        sigma_within, stationarity, vr, sh, chart, model, caveats, var, dep,
    )  # fmt: skip
