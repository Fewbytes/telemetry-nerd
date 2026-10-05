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

from telemetry_nerd.analysis.autocorr import dispersion, n_eff, positions, tau_int, widest
from telemetry_nerd.analysis.excursion import Excursion, excursion
from telemetry_nerd.analysis.sources import COMMON, SPECIAL, UNDETERMINED, cautious_label, item
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
VERDICTS = (
    "insufficient_data", "level_shifted", "transient", "drifting", "periodic", "undetermined",
    "noisy", "stable",
)  # fmt: skip


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
    #: which structure explains the series best: none | trend | step | excursion (a run of
    #: judged points away from the baseline, the centre elsewhere: 7f15)
    model: str = "none"
    caveats: list[str] = field(default_factory=list)
    #: labelled findings (spec §5.4): {source, finding}; measurement-system items are added
    #: from the caveats by the op
    variation: list[dict] = field(default_factory=list)
    departure: Departure | None = None  # events after an all-zero baseline (event counts only)
    #: judged points against the baseline when no control chart judges them or n_eff < 10
    excursion: Excursion | None = None
    #: level shifts significant only under the changepoint test's point model, not the cautious
    #: one the label rests on (principle 16): context, never a label or the step model
    shifts_undetermined: list[Shift] = field(default_factory=list)

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
    undetermined: list[Shift]  # significant under the point model only (changepoints)


def structure(pos, ts_ms, t_s, y, span_ms) -> Structure:
    """Best of constant / linear trend / significant level shifts, by BIC. The step model takes
    the shifts that hold under the changepoint test's cautious model; those only its point model
    sees are kept as undetermined when material against the chosen model's residual sigma
    (none within MIN_SEGMENT of a labelled one)."""
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
    resid = y - fits[model]
    sw = robust_sigma(resid) or float(np.std(resid))
    undetermined = [
        u
        for u in changepoints(pos, ts_ms, y, cautious=False)
        if abs(u.delta) >= MATERIAL * sw
        and all(abs(u.index - s.index) >= MIN_SEGMENT for s in shifts)
    ]
    return Structure(model, tr, shifts, fits[model], resid, undetermined)


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
    changepoint tests. Two models, both reported (principle 16: results are model outputs):

    - Poisson (independent events): given the A events seen, P(none in the n_b baseline steps)
      = (n_j / n)^A, n = n_b + n_j. Exact under its assumption, optimistic when events cluster
      (retries of one request, one fault failing many calls, bursty traffic).
    - clustered (the cautious model): events arrive in independent clusters; the long-run
      variance-to-mean ratio D of the counts (quasi-Poisson phi x integrated autocorrelation
      time) is the clusters' size factor, so ~A / D independent clusters (at least one) carry
      the test: p = (n_j / n)^(A / D). An all-zero baseline has no dispersion to estimate, so D
      is the largest of 1 (Poisson), the judged steps' own D (the only error counts there are;
      they include the departure's own rise and fall, so this over-states clustering under no
      change: a conservative bound) and the live sibling's D (the traffic the events are a
      thinning of: thinning passes on at most its burstiness). The sibling alone is no bound:
      errors can cluster beyond their traffic (retries), so it only raises D.

    The label holds only under the cautious model: special cause when its p < ALPHA,
    undetermined when only the Poisson p is (shown as context), else common cause. Its
    assumption: clusters shorter than the judged window (an error episode as long as the
    window cannot be told from a change by this series alone; a longer zero baseline can)."""

    index: int  # first judged sample with events (descriptive: the test has no split search)
    ts_ms: int
    n_baseline: int
    n_judged: int
    events: int  # judged events, whole (floor: conservative)
    mean: float  # judged mean, the series' units
    interval: tuple[float, float]  # exact Poisson (Garwood) 1 - ALPHA, the series' units
    p: float  # Poisson model (independent events)
    p_clustered: float  # cautious model (clustered events, dispersion D)
    dispersion: float  # D used: max(1, judged, sibling)
    dispersion_source: str  # poisson_floor | judged | sibling
    dispersion_judged: float
    dispersion_sibling: float | None
    clusters: float  # effective independent clusters, A / D (>= 1)

    @property
    def status(self) -> str:
        return cautious_label(self.p_clustered < SPC_ALPHA, self.p < SPC_ALPHA)

    @property
    def significant(self) -> bool:
        """Significant under the cautious model (what a label may rest on)."""
        return self.status == SPECIAL

    def describe(self, fmt=lambda v: f"{v:.3g}") -> str:
        """The two model results, each with its model named (never as a fact)."""
        src = {
            "poisson_floor": "no source shows more than Poisson",
            "judged": "the judged steps' own counts, departure included: a conservative bound",
            "sibling": "the live sibling's traffic",
        }[self.dispersion_source]
        return (
            f"under a Poisson model (independent events) p={self.p:.2g}; allowing clustered "
            f"events (dispersion D={fmt(self.dispersion)} from {src}; ~{self.clusters:.3g} "
            f"independent clusters) p={self.p_clustered:.2g}"
        )


DEPARTURE_METHOD = (
    "events after an all-zero baseline, two models: (a) Poisson (independent events), exact "
    "conditional test of one rate across baseline and judged steps, p = (n_judged / n)^events; "
    "(b) clustered events (cautious): p = (n_judged / n)^(events / D), D the long-run "
    "variance-to-mean ratio (quasi-Poisson phi x autocorrelation time) = max(1, the judged "
    "steps' own D (includes the departure: conservative), the live sibling's D (traffic)), "
    "events / D >= 1 cluster; assumes clusters shorter than the judged window. The source "
    "label rests on (b): special cause when p_clustered < 0.01, undetermined when only (a) is; "
    "judged mean with its exact Poisson (Garwood) 99% interval"
)


def departure_from_zero(
    ts_ms: np.ndarray,
    y: np.ndarray,
    baseline: np.ndarray,
    events_scale: float,
    reference: np.ndarray | None = None,
    sibling: tuple[np.ndarray, np.ndarray] | None = None,
    step_ms: int | None = None,
    exposure: np.ndarray | None = None,
) -> Departure | None:
    """`events_scale`: events per step = value x events_scale. `baseline`: bool per sample;
    `reference`: values of a separately fetched baseline (then every sample is judged).
    `sibling` (ts_ms, y): the live sibling's counts (same instrument, another outcome; the
    series' units). `exposure`: per sample, the traffic the events are a share of (a ratio's
    denominator, wr6j): the judged share of it replaces n_judged / n (one error probability per
    call across baseline and judged steps; ignored with a reference). None unless the baseline
    holds >= MIN_SEGMENT points, all exactly 0, and the judged steps an event."""
    y = np.asarray(y, float)
    ts_ms = np.asarray(ts_ms, np.int64)
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
    step = step_ms or (int(np.min(np.diff(ts_ms))) if ts_ms.size > 1 else 1)
    jts = ts_ms[idx]
    # one judged step: its events may all be one cluster
    d_judged = dispersion(positions(jts, step), judged * events_scale) if nj > 1 else None
    d_judged = float(events) if d_judged is None else d_judged
    d_sib = None
    if sibling is not None and np.asarray(sibling[0]).size > 1:
        sts = np.asarray(sibling[0], np.int64)
        d_sib = dispersion(positions(sts, step), np.asarray(sibling[1], float) * events_scale)
    src, d = widest(
        {"poisson_floor": 1.0, "judged": d_judged, **({"sibling": d_sib} if d_sib else {})}
    )
    clusters = max(1.0, events / d)
    share = nj / (nb + nj)
    if exposure is not None and reference is None:
        ex = np.asarray(exposure, float)
        tot = float(ex.sum())
        if tot > 0 and float(ex[~baseline].sum()) > 0:
            share = float(ex[~baseline].sum()) / tot
    log_f = math.log(share)
    p = math.exp(events * log_f)
    p_c = math.exp(clusters * log_f)
    lo, hi = poisson_interval(events, SPC_ALPHA)
    unit = nj * events_scale
    first = int(idx[int(np.flatnonzero(judged > 0)[0])])
    return Departure(
        first, int(ts_ms[first]), nb, nj, events, float(judged.mean()), (lo / unit, hi / unit), p,
        p_c, d, src, d_judged, d_sib, clusters,
    )  # fmt: skip


def _two_model_reason(source: str, reasons: list[str], var: list[dict], why: dict) -> None:
    """The reason and variation item of a test judged under two models: its source (the
    cautious model's label) picks the text."""
    reasons.append(why[source])
    var.append(item(source, why[source]))


def _baseline_calm(b_ts: np.ndarray, b_pos: np.ndarray, b_y: np.ndarray) -> Excursion | None:
    """Self-check (bead k9sn): is the baseline itself calm? The control limits trust it as the
    common-cause reference, but either end can hold the tail of an earlier, different episode
    (e.g. the previous scenario's last points still elevated). Split the baseline at its
    midpoint both ways (first half as the reference / second half judged, and back) and scan
    each for an excursion (same test as the judged window, bead 7f15: median/MAD, Bonferroni
    over every run), so contamination at either end is caught regardless of which half it falls
    in. None when there are too few baseline points to test (< 2 x MIN_SEGMENT) or neither split
    finds one."""
    n = b_y.size
    if n < 2 * MIN_SEGMENT:
        return None
    mid = n // 2
    for ref_first in (True, False):
        sub = np.zeros(n, bool)
        sub[:mid] = ref_first
        sub[mid:] = not ref_first
        exc = excursion(b_ts, b_pos, b_y, sub)
        if exc is not None and exc.status != COMMON:
            return exc
    return None


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
    sibling: tuple[np.ndarray, np.ndarray] | None = None,
    counts: np.ndarray | None = None,
    exposure: np.ndarray | None = None,
) -> Diagnosis:
    """`baseline`: bool per sample (SPC limits come only from these). `sp`: the series'
    spectrum, or None when the range is too short to resolve any period.

    `reference` (ts_ms, y): a separately fetched baseline (an earlier window, e.g. the same
    hours last week); when given, SPC limits come only from it and every point of the series
    is judged (`baseline` is ignored). `profile` (shape, cycle_s): the operating profile's
    seasonal shape per sample of [reference..., series...], estimated without the series.
    `events_scale`: the series counts events (value x events_scale = events per step, e.g.
    increase() of a counter); then a baseline that saw none is tested for a departure from
    zero (`Departure`); `sibling` (ts_ms, y): the live sibling's counts, a dispersion source
    for its cautious model. `counts` (per sample): the event series behind `y` when `y` is a
    ratio of them (its numerator, wr6j): the departure is tested on it, `events_scale` its
    scale and `exposure` (the denominator, per sample) the traffic it is a share of.

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
    if reference is not None:
        b_ts = np.asarray(reference[0], np.int64)
        b_y = np.asarray(reference[1], float)
        b_pos = positions(b_ts, step_ms) if b_ts.size else b_ts
    else:
        b_ts, b_pos, b_y = ts_ms[baseline], pos[baseline], y[baseline]
    if (calm := _baseline_calm(b_ts, b_pos, b_y)) is not None and calm.status != COMMON:
        caveats.append("baseline_not_calm")

    labels: list[str] = []
    reasons: list[str] = []
    var: list[dict] = []
    dep = (
        departure_from_zero(
            ts_ms,
            y if counts is None else np.asarray(counts, float),
            baseline,
            events_scale,
            None if reference is None else reference[1],
            sibling,
            step_ms,
            exposure,
        )
        # a ratio's numerator has no reference counts to test against
        if events_scale and (counts is None or reference is None)
        else None
    )
    departed = dep is not None and dep.significant
    # a departure only the Poisson model sees is not labelled, but not stable or noise either
    dep_tested = dep is not None and dep.status != COMMON
    # no chart judges the judged points (short baseline), or the series' residuals look too
    # dependent: test them against the baseline's own variation (an episode that fits neither
    # step nor trend inflates tau through its own rise and fall, 7f15)
    exc = None
    if chart.mode == "insufficient_data" or ne < MIN_N_EFF:
        ref = None
        if reference is not None:
            rts = np.asarray(reference[0], np.int64)
            ref = (positions(rts, step_ms) if rts.size else rts, reference[1])
        exc = excursion(ts_ms, pos, y, baseline, ref)
    exc_special = exc is not None and exc.significant
    exc_tested = exc is not None and exc.status != COMMON
    if exc is not None and exc_special and ne < MIN_N_EFF:
        # the excursion is the structure the residuals still held: their autocorrelation and
        # sigma from what it leaves
        run = np.zeros(n, bool)
        run[exc.start : exc.end] = True
        if reference is None:
            run &= ~baseline
        fitted = np.full(n, exc.centre)
        fitted[run] = float(yd[run].mean())
        resid = yd - fitted
        model = "excursion"
        sigma_within = robust_sigma(resid) or float(np.std(resid))
        tau = tau_int(pos, resid)
        ne = n_eff(n, tau)
    if ne < MIN_N_EFF:
        # the judged points were tested against the baseline (a departure from zero or an
        # excursion, under two models): that is not "insufficient data", whatever the label
        reasons.append(
            f"n_eff {ne:.1f} < {MIN_N_EFF}: {n} points but autocorrelation time {tau:.1f} steps"
            + (": trend, shift and period tests are unreliable; the departure from the zero "
               "baseline rests on the event counts alone" if dep_tested else "")
            + (": trend, shift and period tests are unreliable; the excursion test judges the "
               "judged points against the baseline's own variation"
               if exc_tested and not dep_tested else "")
        )  # fmt: skip
        if not (dep_tested or exc_tested):
            labels.append("insufficient_data")
    material = [s for s in shifts if abs(s.delta) >= MATERIAL * sigma_within]
    if material and model == "step":
        labels.append("level_shifted")
        for s in material:
            reasons.append(
                f"level shift {fmt(s.delta)} [{fmt(s.interval[0])}, {fmt(s.interval[1])}] at "
                f"{fmt_ts(s.ts_ms)} ({abs(s.delta) / sigma_within:.1f} sigma"
                f"{', small' if abs(s.delta) < SMALL * sigma_within else ''}, p={s.p_cautious:.1g})"
            )
            var.append(item(SPECIAL, reasons[-1]))
    for s in shifts:
        if s not in material:
            reasons.append(
                f"minor shift {fmt(s.delta)} at {fmt_ts(s.ts_ms)} (p={s.p_cautious:.1g}, "
                f"{abs(s.delta) / sigma_within:.1f} sigma < {MATERIAL:g})"
            )
            var.append(item(SPECIAL, reasons[-1]))
    # seen by the point model only (principle 16): reported, the source not decided
    undet = [s for s in final.undetermined if abs(s.delta) >= MATERIAL * sigma_within]
    for s in undet:
        _two_model_reason(UNDETERMINED, reasons, var, {UNDETERMINED: (
            f"level shift {fmt(s.delta)} [{fmt(s.interval[0])}, {fmt(s.interval[1])}] at "
            f"{fmt_ts(s.ts_ms)} ({abs(s.delta) / sigma_within:.1f} sigma) under the point AR(1) "
            f"model only (p_point={s.p:.1g}, p_cautious={s.p_cautious:.1g}); on this effective "
            f"sample size (n_eff {ne:.0f}) the source is not decided (a longer range would "
            "decide it)"
        )})  # fmt: skip
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
            f"[{fmt(dep.interval[0])}, {fmt(dep.interval[1])}] per step"
            f"{' in the numerator units of the ratio' if counts is not None else ''}); "
            f"{dep.describe(fmt)}"
        )
        if departed and "level_shifted" not in labels:
            labels.append("level_shifted")
        _two_model_reason(dep.status, reasons, var, {
            SPECIAL: f"departure from a zero baseline under both models: {what}",
            UNDETERMINED: (
                f"departure from a zero baseline under the Poisson model only: {what}; one "
                "burst of clustered events cannot be told from a change at this baseline length "
                "(a longer zero baseline, e.g. a range starting earlier, would decide it)"
            ),
            COMMON: f"events after a zero baseline, within what one rate explains: {what}",
        })  # fmt: skip
    if exc is not None:
        what = (
            f"{exc.points} judged point(s) {fmt_ts(exc.start_ms)}..{fmt_ts(exc.end_ms)} "
            f"{'then back' if exc.returned else '(still away at the end of the range)'}: mean "
            f"{fmt(exc.mean)} "
            f"vs baseline median {fmt(exc.centre)}, delta {fmt(exc.delta)} "
            f"[{fmt(exc.interval[0])}, {fmt(exc.interval[1])}] ({exc.sigmas:.3g} sigma); "
            f"{exc.describe()}"
        )
        lab = "transient" if exc.returned else "level_shifted"
        if exc_special and lab not in labels:
            labels.append(lab)
        _two_model_reason(exc.status, reasons, var, {
            SPECIAL: f"excursion from the baseline under both models: {what}",
            UNDETERMINED: (
                f"excursion from the baseline under the baseline model only: {what}; the "
                "cautious model (heavier tails, the residuals' own autocorrelation and spread) "
                "explains it as common cause, so the source is not decided (a longer baseline, "
                "e.g. a range starting earlier, would decide it)"
            ),
            COMMON: f"judged points within the baseline's common-cause variation: {what}",
        })  # fmt: skip
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
        dep_tested
        or exc_tested
        or bool(undet)
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
    changed = {"insufficient_data", "level_shifted", "transient", "drifting"}
    undecided = bool(undet) or UNDETERMINED in (
        dep.status if dep else None,
        exc.status if exc else None,
    )
    if undecided and not changed & set(labels):
        labels.append("undetermined")  # tested, the models disagree: not "stable"
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
        sigma_within, stationarity, vr, sh, chart, model, caveats, var, dep, exc,
        undet,
    )  # fmt: skip
