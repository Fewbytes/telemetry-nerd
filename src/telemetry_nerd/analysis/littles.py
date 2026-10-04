"""Little's law check: measured mean concurrency L against throughput x mean latency (czt.2, 60j).

Pure numpy over aligned sub-steps. Design: docs/superpowers/specs/2026-10-02-littles-law-design.md.

Per window and pooled: the discrepancy L - lambda W and R = L / (lambda W), always reported. Its
MEASUREMENT interval holds only what the instruments add for this window (gauge sampling, the
steady-state straddle at the window edges, counter scrape timing, the rate() lookback): over a
window, L T and lambda W T describe the same realised requests, so the counts' Poisson noise is
not an error of the comparison. That noise is the COMMON-CAUSE scale (a small system's per-window
aggregates do not average out), reported separately to qualify window differences.

Variation is labelled by source (SPC):
- measurement system: the measurement interval, and a SYSTEMATIC offset (persistent L != lambda W:
  unmeasured queueing, a missing instance, units, latency on a subset/superset);
- common cause: within the small-system envelope (+-X per window): not to be chased;
- undetermined: with fewer than MIN_SPREAD windows (no estimate of the process's own variation),
  beyond the small-system (Poisson) envelope but not the cautious one (its two terms inflated by
  the window's own sub-step arrival dispersion and latency variation): principle 16, the label
  rests on the cautious model, both are reported;
- special cause: TRANSIENT windows beyond both, against the systematic level (load peaks, leaving
  steady state, a change in the instrumentation); and a window at a LOAD PEAK inside the envelope
  (or the measurement interval) PROMOTED by independent evidence of leaving steady state (83w,
  q2m): the backlog growing (the gauge, and arrivals - completions when lambda counts arrivals),
  W rising into / through the peak, or the deviation growing across repeated peaks. Without that
  evidence a load-peak window inside the envelope stays common cause: not a signal by itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from telemetry_nerd.analysis.autocorr import n_eff, tau_int
from telemetry_nerd.analysis.sources import (
    COMMON,
    MEASUREMENT,
    SPECIAL,
    UNDETERMINED,
    cautious_label,
)
from telemetry_nerd.analysis.stability import trend
from telemetry_nerd.analysis.stats import t_quantile

ALPHA = 0.05
MIN_SUBSTEPS = 4  # fewer usable sub-steps: the window is not judged
STEADY_CHANGE = 0.10  # a significant trend moving L or lambda by more than this is "not steady"
COMMON_CAUSE_WARN = 0.10  # common-cause half-width (95%, relative) from which the warning is raised
PEAK_LAMBDA = 1.1  # a window's lambda this far above the median window: a load peak
PEAK_W = 1.5  # a window's W this far above the median window: a latency surge
MAX_ITER = 6
PROMOTE_ALPHA = 0.05  # family-wise false promotions per check (its own budget, apart from alpha)
MIN_BASELINE = 4  # windows outside a peak's episode needed to scale its backlog / W tests
#: judged windows needed to estimate the process's own window-to-window variation (`spread`);
#: fewer: the cautious envelope (Block.common_cautious) carries the special-cause label (4ahp)
MIN_SPREAD = 5


@dataclass(frozen=True)
class Substeps:
    """Aligned per-sub-step measurements of one group. NaN = not observed.

    Rates are per second; lat_sum is latency-seconds per second (already in seconds)."""

    ts_ms: np.ndarray  # bucket END times, ascending, on one grid
    arrivals: np.ndarray
    lat_sum: np.ndarray
    lat_count: np.ndarray
    conc: np.ndarray  # time-average of the gauge over the sub-step
    conc_n: np.ndarray  # gauge samples behind each sub-step
    step_ms: int
    lookback_ms: int = 0  # rate window offset vs the gauge average: (rate interval - step) / 2
    scrape_ms: int = 0  # counter scrape interval (0: the sub-step)

    def usable(self) -> np.ndarray:
        return (
            np.isfinite(self.arrivals)
            & np.isfinite(self.lat_sum)
            & np.isfinite(self.lat_count)
            & np.isfinite(self.conc)
        )


@dataclass
class Block:
    """One judged span (a window, the whole range, or the windows of a systematic offset)."""

    start_ms: int
    end_ms: int
    n: int
    verdict: str  # consistent | L_high | L_low | insufficient | no_traffic
    L: float | None = None
    L_ci: tuple[float, float] | None = None
    lam: float | None = None
    W_s: float | None = None
    lambda_W: float | None = None
    lambda_W_ci: tuple[float, float] | None = None
    ratio: float | None = None
    ci95: tuple[float, float] | None = None  # measurement interval (pointwise 95%)
    ci_test: tuple[float, float] | None = None  # measurement interval at the verdict's level
    diff: float | None = None  # L - lambda W, requests
    diff_ci: tuple[float, float] | None = None
    common: float | None = None  # common-cause half-width of L and lambda W, relative, 95%
    #: the same under the cautious model (4ahp): each term inflated by the window's own
    #: sub-steps (arrivals' long-run dispersion, completions' effective latency CV), >= common
    common_cautious: float | None = None
    common_terms: dict = field(default_factory=dict)  # k_arrivals, k_latency (1: Poisson)
    sd: float | None = None
    sd_source: str | None = None  # empirical | floor: which bounds the gauge sampling error
    sd_terms: dict = field(default_factory=dict)  # each measurement component's sd, ratio units
    bias: dict = field(default_factory=dict)
    arrivals: float | None = None  # count in the span
    completions: float | None = None
    gauge_samples: int | None = None
    L_edges: tuple[float, float] | None = None  # gauge at the first and last sub-step
    flags: list[str] = field(default_factory=list)
    drift: dict = field(default_factory=dict)
    flow: dict | None = None
    reason: str | None = None
    #: deviation from the reference: MEASUREMENT | COMMON | UNDETERMINED | SPECIAL
    source: str | None = None
    promotion: dict | None = None  # why a load-peak window was promoted to special cause
    L_correction: float = 0.0  # added to the gauge average: its end-of-interval reading (trapezoid)


@dataclass
class GroupResult:
    windows: list[Block]
    pooled: Block  # the whole range
    verdict: str  # consistent | L_high | L_low | inconsistent_in_windows | insufficient
    flagged: list[int]  # windows whose measurement interval (family-wise) excludes 1
    growing: dict | None = None  # trend of L - lambda W over windows (leak / backlog)
    reference: float = 1.0  # the level windows are compared with: 1, or the systematic offset
    references: dict[int, float] = field(default_factory=dict)  # per window (a drifting offset)
    # pooled over the non-transient steady windows (or, when most windows are off 1 on one side
    # or not steady, all of them), when it excludes 1
    systematic: Block | None = None
    basis: list[int] = field(default_factory=list)  # the windows behind `systematic`
    core: list[int] = field(default_factory=list)  # the windows not transient
    transient: list[dict] = field(default_factory=list)
    common_cause: dict = field(default_factory=dict)
    phases: dict[int, dict] = field(default_factory=dict)  # load context of every judged window
    promoted: list[dict] = field(default_factory=list)  # load-peak windows promoted to special
    promotion: dict = field(default_factory=dict)  # the promotion tests' levels and thresholds
    offset: GroupResult | None = None  # the same check on the grid shifted by half a window


def _sampling_factor(dt_s: float, w_s: float) -> float:
    """Error variance of a time average sampled every dt, relative to (variance / samples), for
    exponentially correlated fluctuations with correlation time w: coth(x/2) - 2/x, x = dt/w.
    1 when samples are independent (dt >> w), x/6 when the path is well resolved (dt << w)."""
    if w_s <= 0:
        return 1.0
    x = dt_s / w_s
    if x > 50:
        return 1.0
    if x < 1e-3:
        return x / 6
    return 1 / math.tanh(x / 2) - 2 / x


def _mean(x: np.ndarray) -> float:
    return float(np.mean(x)) if x.size else math.nan


def _drift(pos: np.ndarray, ts: np.ndarray, y: np.ndarray, span_ms: int) -> dict | None:
    if y.size < 6 or float(np.std(y)) == 0:
        return None
    tr = trend(pos, ts, y, span_ms)
    mean = abs(float(np.mean(y))) or 1.0
    rel = tr.change / mean
    rel_ci = (tr.change_interval[0] / mean, tr.change_interval[1] / mean)
    lo = min(abs(rel_ci[0]), abs(rel_ci[1])) if tr.significant else 0.0
    return {
        "change": rel,
        "change_ci": rel_ci,
        "significant": tr.significant,
        "material": tr.significant and lo > STEADY_CHANGE,
    }


def _own_scale(
    pos: np.ndarray, a: np.ndarray, s: np.ndarray, c: np.ndarray, W: float, CT: float,
    step_s: float,
) -> tuple[float, float]:  # fmt: skip
    """The small-system envelope's two terms as the window's own sub-steps show them, relative
    to the Poisson model (1 = Poisson arrivals, latency CV 1; never below: it can only widen the
    envelope). Arrivals: sqrt of the counts' long-run variance-to-mean ratio (clustered or bursty
    arrivals). Latency: the window mean W's long-run relative sd from e = latency-seconds - W x
    completions per sub-step (slow requests in bursts) x sqrt(completions): the effective latency
    CV. Both around the window's own linear trend (a load or latency ramp inside the window is
    the change being judged, not its noise; `not_steady` flags it), x the residuals'
    autocorrelation time."""
    t = pos.astype(float)

    def long_run_var(x: np.ndarray) -> float:
        if x.size < 3:
            return 0.0
        r = x - np.polyval(np.polyfit(t, x, 1), t)
        return float(r @ r) / (x.size - 2) * tau_int(pos, r)

    n = a.size
    events = a * step_s
    k_lam = 1.0
    if events.sum() > 0:
        k_lam = max(1.0, math.sqrt(long_run_var(events) / float(events.mean())))
    k_w = 1.0
    if W > 0 and CT > 0:
        k_w = max(1.0, math.sqrt(long_run_var((s - W * c) * step_s) * n / CT) / W)
    return k_lam, k_w


def _small_system(
    q95: float, arrivals: float, completions: float, k_lam: float = 1.0, k_w: float = 1.0
) -> float:
    """The small-system envelope of L / (lambda W) - 1 at q95: Poisson arrival and completion
    counts, each term inflated by its own factor (1: Poisson, latency CV 1; the window's own
    sub-step variation: the cautious envelope, 4ahp)."""
    return q95 * (k_lam / math.sqrt(max(arrivals, 1.0)) + k_w / math.sqrt(max(completions, 1.0)))


def _endpoint_shift(sub: Substeps, ok: np.ndarray) -> float:
    """Sum over the contiguous runs of `ok` of N at the run's last sub-step minus N just before
    it (the sub-step before the run when observed, else its first)."""
    if ok.size == 0:
        return 0.0
    cuts = np.flatnonzero(np.diff(sub.ts_ms[ok]) > sub.step_ms * 1.5) + 1
    total = 0.0
    for run in np.split(ok, cuts):
        before = int(run[0]) - 1
        g0 = sub.conc[before] if before >= 0 and np.isfinite(sub.conc[before]) else sub.conc[run[0]]
        total += float(sub.conc[run[-1]] - g0)
    return total


def _segments(sub: Substeps, ok: np.ndarray) -> int:
    """Contiguous runs of sub-steps in `ok` (a pool of non-adjacent windows has several edges)."""
    if ok.size == 0:
        return 0
    gaps = np.diff(sub.ts_ms[ok]) > sub.step_ms * 1.5
    return 1 + int(np.count_nonzero(gaps))


def judge(
    sub: Substeps,
    idx: np.ndarray,
    q_test: float | None = None,
    alpha: float = ALPHA,
) -> Block:
    """L vs lambda W over the sub-steps `idx` (indexes into `sub`). `q_test`: the quantile for the
    verdict interval (multiplicity-adjusted by the caller); default the pointwise 1 - alpha/2."""
    step_s = sub.step_ms / 1000
    ok = idx[sub.usable()[idx]]
    start = int(sub.ts_ms[idx[0]]) - sub.step_ms if idx.size else 0
    end = int(sub.ts_ms[idx[-1]]) if idx.size else 0
    n = int(ok.size)
    if n < MIN_SUBSTEPS:
        return Block(
            start, end, n, "insufficient", reason=f"{n} usable sub-steps (< {MIN_SUBSTEPS})"
        )
    pos = ((sub.ts_ms[ok] - sub.ts_ms[ok][0]) // sub.step_ms).astype(np.int64)
    a, s, c, g = sub.arrivals[ok], sub.lat_sum[ok], sub.lat_count[ok], sub.conc[ok]
    T = n * step_s
    L, lam, S, C = _mean(g), _mean(a), _mean(s), _mean(c)
    samples = int(np.nansum(sub.conc_n[ok]))
    blk = Block(start, end, n, "insufficient", L=L, lam=lam, gauge_samples=samples)
    blk.arrivals, blk.completions = lam * T, C * T
    blk.L_edges = (float(g[0]), float(g[-1]))
    if lam <= 0 or C <= 0 or S <= 0:
        blk.verdict = "no_traffic"
        blk.reason = "no arrivals or completions in the window: lambda W is 0"
        if L > 0:
            blk.flags.append("in_flight_without_traffic")
        return blk
    # the gauge is read at the END of each scrape interval the counters cover ((s_j-1, s_j]):
    # the readings' sum integrates N half a scrape late, off by (N at the end - N before) x
    # scrape / 2 per contiguous segment (exact for a linear path): corrected (trapezoid), not
    # bounded: its sign is known
    gap_s = (sub.scrape_ms or sub.step_ms) / 1000
    shift = _endpoint_shift(sub, ok) * gap_s / 2 / T
    L = max(L - shift, 0.0)
    blk.L, blk.L_correction = L, -shift
    W = S / C
    lw = lam * W
    R = L / lw
    blk.W_s, blk.lambda_W, blk.ratio, blk.diff = W, lw, R, L - lw
    segs = _segments(sub, ok)
    df = max(n - 1, 1)
    q95 = t_quantile(1 - alpha / 2, df)

    # MEASUREMENT interval: what the instruments add for this window -------------------------
    # 1. gauge sampling: the gauge's time average from m scrape samples vs the path's true time
    # average. Successive differences estimate it from the data (a fast process: variance / m; a
    # slow one: small); the floor is a Poisson-occupancy process with correlation time W under
    # this sampling: what scrapes can miss between them (short spikes) when every sample looks
    # alike.
    m = max(samples, n)
    dg = np.diff(g)
    var_emp = float(dg @ dg) / (2 * max(dg.size, 1)) / n if dg.size else math.inf
    var_floor = max(L, lw) * _sampling_factor(sub.step_ms / 1000 * n / m, W) / m
    sd_L = math.sqrt(max(var_emp, var_floor))
    # 2. edge straddle: L T counts the in-window part of requests in flight at the edges, lambda
    # W T their whole latency. In steady state the two edges cancel on average; what remains is
    # random: per edge a Poisson(L) number of requests, each with a residual time of second
    # moment 2 W^2 (CV 1); W from the sub-steps' completion-weighted RMS (latency variation in the
    # window widens it). A backlog building or draining (not steady state) is not in it: that is
    # a real, special-cause discrepancy.
    w_sub = np.where(c > 0, s / np.where(c > 0, c, 1.0), np.nan)
    fin = np.isfinite(w_sub) & (c > 0)
    w_rms = math.sqrt(float(np.sum(c[fin] * w_sub[fin] ** 2) / np.sum(c[fin]))) if fin.any() else W
    sd_edge = 2 * max(w_rms, W) * math.sqrt(segs * max(L, lw)) / (lw * T)
    # 3. counter scrape timing: increments between a window edge and the nearest scrape land in
    # the neighbouring window (rate() extrapolates over them): at most one scrape interval per
    # edge, Poisson in that fragment, for the arrival counter and the latency count/sum (CV 1);
    # correlation unknown, so lambda's and W's add linearly.
    r_lam = math.sqrt(2 * segs * lam * gap_s) / max(lam * T, 1e-12)
    r_w = math.sqrt(2 * segs * C * gap_s) / max(C * T, 1e-12)
    sd_count = R * (r_lam + r_w)
    sd = math.sqrt((sd_L / lw) ** 2 + sd_edge**2 + sd_count**2)
    blk.sd, blk.sd_source = sd, "empirical" if var_emp >= var_floor else "floor"
    blk.sd_terms = {
        "gauge_sampling": sd_L / lw, "edge_straddle": sd_edge, "scrape_timing": sd_count,
    }  # fmt: skip
    # 4. rate() lookback (a bias bound: added to the half-width, never in quadrature): with
    # rate(x[ri]) (Prometheus; not with VM's increase() tiles, lookback 0) each sub-step's counters
    # reach back delta further than the gauge reading, so the window's counts are those of a
    # window shifted by delta: off by delta x (rate at the start - rate at the end), each end's
    # rate from the sub-steps one rate interval covers, plus that estimate's own counting noise
    delta_s = sub.lookback_ms / 1000
    j = max(1, min(n // 2, round((2 * delta_s + step_s) / step_s)))

    def edge_change(x: np.ndarray, mean: float, per_s: float) -> float:
        if mean <= 0:
            return 0.0
        noise = math.sqrt(2 / max(per_s * j * step_s, 1.0))
        return abs(float(np.mean(x[-j:]) - np.mean(x[:j]))) / mean + noise

    align = (
        R * (delta_s / T) * max(edge_change(a, lam, lam), edge_change(s, S, C),
                                edge_change(c, C, C))
        if delta_s > 0 else 0.0
    )  # fmt: skip
    blk.bias = {"alignment": align}
    bias = align
    qt = q95 if q_test is None else t_quantile(q_test, df)
    blk.ci95 = (max(0.0, R - q95 * sd - bias), R + q95 * sd + bias)
    blk.ci_test = (max(0.0, R - qt * sd - bias), R + qt * sd + bias)
    if blk.ci_test[0] > 1:
        blk.verdict = "L_high"
    elif blk.ci_test[1] < 1:
        blk.verdict = "L_low"
    else:
        blk.verdict = "consistent"
    blk.diff_ci = ((blk.ci95[0] - 1) * lw, (blk.ci95[1] - 1) * lw)
    # per-side intervals (panel bands)
    half_L = q95 * math.hypot(sd_L, sd_edge * lw)
    blk.L_ci = (max(0.0, L - half_L), L + half_L)
    half_lw = lw * q95 * (r_lam + r_w) + align * lw / max(R, 1e-12)
    blk.lambda_W_ci = (max(0.0, lw - half_lw), lw + half_lw)
    # COMMON CAUSE: a small system's per-window L and lambda W fluctuate with the number of
    # requests behind them (Poisson counts, latency CV 1): not measurement error, reported apart
    blk.common = _small_system(q95, lam * T, C * T)
    k_lam, k_w = _own_scale(pos, a, s, c, W, C * T, step_s)
    blk.common_terms = {"k_arrivals": k_lam, "k_latency": k_w}
    blk.common_cautious = _small_system(q95, lam * T, C * T, k_lam, k_w)

    # assumptions
    span = end - start
    for name, y in (("L", g), ("lambda", a)):
        d = _drift(pos, sub.ts_ms[ok], y, span)
        if d is not None:
            blk.drift[name] = d
    if any(d["material"] for d in blk.drift.values()):
        blk.flags.append("not_steady")
    if W > T / 10:
        blk.flags.append("window_short_vs_latency")
    if samples < 10:
        blk.flags.append("few_gauge_samples")
    blk.flow = _flow(g, lam, C, T, q95, L, lw)
    if blk.flow["imbalanced"]:
        blk.flags.append("flow_imbalance")
    return blk


def _flow(g, lam, C, T, q, L, lw) -> dict:
    """Arrival counter vs latency count: the same requests? Over a window they differ by the
    backlog change (gauge at the end minus at the start) plus counting noise."""
    A, D = lam * T, C * T
    diff = A - D - float(g[-1] - g[0])
    sd = math.sqrt(A + D) + math.sqrt(2 * max(L, lw))
    return {
        "ratio": A / D,
        "unexplained": diff,
        "tolerance": q * sd,
        "imbalanced": abs(diff) > q * sd,
    }


def window_indexes(sub: Substeps, k: int, skip: int = 0) -> list[np.ndarray]:
    """Consecutive windows of k sub-steps after `skip`; a trailing part of >= k/2 is kept."""
    n = sub.ts_ms.size
    out = []
    for a in range(skip, n, k):
        b = min(a + k, n)
        if b - a >= max(1, k // 2):
            out.append(np.arange(a, b))
    return out


def check(
    sub: Substeps,
    k: int,
    skip: int = 0,
    tests: int | None = None,
    alpha: float = ALPHA,
    arrivals: str = "unknown",
    offset: bool = True,
) -> GroupResult:
    """Windows of k sub-steps and the pooled span after `skip` sub-steps of warm-up.

    alpha is split: alpha/2 for the systematic offset (pooled over the windows that are not
    transient and are in steady state, see _classify), alpha/2 family-wise (Bonferroni over `tests`, default this group's windows) for
    the window tests (against 1 and against the systematic level). Promotion of load-peak
    windows has its own family-wise budget, PROMOTE_ALPHA (see _promote). `arrivals`: what the
    counter counts (arrivals | completions | unknown): with arrivals, arrivals - completions is a
    second reading of the backlog.

    `offset` (xa4): a load episode (backlog building and draining) that starts and ends inside
    one window balances over it — Little's law holds over that window — so where it falls on
    the grid decides whether it is seen. The windows are therefore judged on a second grid
    shifted by half a window as well (the window tests and promotions Bonferroni over both
    grids: 2 x `tests`); special-cause windows of the shifted grid that no special-cause window
    of the main grid covers are added (`grid: offset`, with their own span and reference). An
    episode shorter than half a window can still fall inside a window of both grids."""
    m = tests or max(1, len(window_indexes(sub, k, skip)))
    shifted = offset and k >= 2 and sub.ts_ms.size - skip - k // 2 >= k
    m_all = 2 * m if shifted else m
    out = _check_grid(sub, k, skip, m_all, alpha, arrivals)
    if not shifted or out.verdict in ("no_traffic", "insufficient"):
        return out
    _merge_offset(out, _check_grid(sub, k, skip + k // 2, m_all, alpha, arrivals))
    return out


def _check_grid(
    sub: Substeps, k: int, skip: int, m: int, alpha: float, arrivals: str
) -> GroupResult:
    wins = window_indexes(sub, k, skip)
    q_win = 1 - alpha / (4 * m)
    windows = [judge(sub, w, q_win, alpha) for w in wins]
    allidx = np.arange(skip, sub.ts_ms.size)
    pooled = (
        judge(sub, allidx, 1 - alpha / 4, alpha) if allidx.size else Block(0, 0, 0, "insufficient")
    )
    flagged = [i for i, w in enumerate(windows) if w.verdict in ("L_high", "L_low")]
    out = GroupResult(windows, pooled, pooled.verdict, flagged, _growing(windows))
    judged = [i for i, w in enumerate(windows) if w.ratio is not None and w.sd is not None]
    if not judged:
        out.verdict = "no_traffic" if pooled.verdict == "no_traffic" else "insufficient"
        return out
    _classify(out, sub, wins, judged, q_win, alpha)
    _promote(out, sub, wins, judged, m, arrivals)
    for t in out.transient:
        t["cause"] = _cause(t)
    if out.systematic is not None:
        out.verdict = out.systematic.verdict
    elif out.transient:
        out.verdict = "inconsistent_in_windows"
    else:
        out.verdict = "consistent"
    _common_cause(out, judged)
    return out


def _merge_offset(out: GroupResult, off: GroupResult) -> None:
    """Special-cause windows of the half-window-shifted grid that no special-cause window of the
    main grid overlaps: added to the transient / promoted lists with their own block (span,
    ratio, interval) and reference, marked `grid: offset`; they make the verdict
    inconsistent_in_windows when the main grid alone is consistent (promotions do not: the
    verdict is about L = lambda W)."""
    special = [
        (w.start_ms, w.end_ms) for w in out.windows if w.source == SPECIAL and w.ratio is not None
    ]
    covered = lambda w: any(a < w.end_ms and w.start_ms < b for a, b in special)
    added = False
    for t in off.transient:
        w = off.windows[t["index"]]
        if t["source"] != SPECIAL or covered(w):
            continue
        ref = off.references.get(t["index"], off.reference)
        out.transient.append({**t, "index": None, "grid": "offset", "block": w, "reference": ref})
        added = added or not t["promoted"]
    for p in off.promoted:
        w = off.windows[p["index"]]
        if covered(w):
            continue
        ref = off.references.get(p["index"], off.reference)
        out.promoted.append({**p, "index": None, "grid": "offset", "block": w, "reference": ref})
    out.transient.sort(key=lambda t: _span_of(out, t)[0])
    out.promoted.sort(key=lambda p: _span_of(out, p)[0])
    out.offset = off
    if added and out.verdict == "consistent":
        out.verdict = "inconsistent_in_windows"


def _span_of(g: GroupResult, t: dict) -> tuple[int, int]:
    w = t.get("block") or g.windows[t["index"]]
    return w.start_ms, w.end_ms


def window_of(g: GroupResult, t: dict) -> Block:
    """The block of a transient / promoted entry: its own (shifted grid) or the main grid's."""
    return t.get("block") or g.windows[t["index"]]


def _bias(w: Block) -> float:
    """A block's bias bounds (rate lookback, gauge endpoint), added to its half-width."""
    return float(sum(w.bias.values()))


def _off(w: Block, ref: float, ref_sd: float, ref_bias: float, q: float) -> bool:
    """Does window w differ from the reference level beyond the measurement interval?"""
    assert w.ratio is not None and w.sd is not None
    half = t_quantile(q, max(w.n - 1, 1)) * math.hypot(w.sd, ref_sd)
    return abs(w.ratio - ref) > half + _bias(w) + ref_bias


def _level(
    sub: Substeps, wins: list[np.ndarray], ws: list[Block], core: list[int], alpha: float
) -> Block:
    """The level the `core` windows share: their ratios averaged with equal weights (one heavy
    window cannot make an offset persistent), its standard error the larger of the measurement
    one and the windows' own spread (window-to-window variation is part of the level's
    uncertainty); t over the windows, at alpha/2 for the verdict. Fewer than 3 windows: the
    sub-steps pooled."""
    if len(core) < 3:
        return judge(sub, np.concatenate([wins[i] for i in core]), 1 - alpha / 4, alpha)
    b = [ws[i] for i in core]
    r = np.array([w.ratio for w in b], float)
    n = r.size
    R = float(r.mean())
    se_meas = math.sqrt(sum(float(w.sd) ** 2 for w in b)) / n  # type: ignore[arg-type]
    se_spread = float(r.std(ddof=1)) / math.sqrt(n)
    se = max(se_meas, se_spread)
    align = float(np.mean([_bias(w) for w in b]))
    # df: the windows' when their spread sets the error, else the sub-steps' behind the
    # measurement sds
    df = n - 1 if se_spread > se_meas else sum(w.n for w in b) - n
    q95, qt = t_quantile(1 - alpha / 2, df), t_quantile(1 - alpha / 4, df)
    L = float(np.mean([w.L for w in b]))
    lw = float(np.mean([w.lambda_W for w in b]))
    out = Block(b[0].start_ms, b[-1].end_ms, sum(w.n for w in b), "consistent", L=L, ratio=R)
    out.lam = float(np.mean([w.lam for w in b]))
    out.W_s = float(np.mean([w.W_s for w in b]))
    out.lambda_W, out.diff, out.sd = lw, L - lw, se
    out.sd_source = "spread" if se_spread > se_meas else "measurement"
    out.sd_terms = {"measurement": se_meas, "between_windows": se_spread}
    out.bias = {"alignment": align}
    out.ci95 = (max(0.0, R - q95 * se - align), R + q95 * se + align)
    out.ci_test = (max(0.0, R - qt * se - align), R + qt * se + align)
    out.diff_ci = ((out.ci95[0] - 1) * lw, (out.ci95[1] - 1) * lw)
    out.arrivals = float(sum(w.arrivals or 0 for w in b))
    out.completions = float(sum(w.completions or 0 for w in b))
    out.common = q95 * (
        1 / math.sqrt(max(out.arrivals, 1.0)) + 1 / math.sqrt(max(out.completions, 1.0))
    )
    if out.ci_test[0] > 1:
        out.verdict = "L_high"
    elif out.ci_test[1] < 1:
        out.verdict = "L_low"
    return out


def _classify(
    out: GroupResult, sub: Substeps, wins: list[np.ndarray], judged: list[int], q: float,
    alpha: float,
) -> None:  # fmt: skip
    """Systematic offset vs transient windows. Start from the median window ratio (robust to a
    few transients), mark the windows off that level beyond their measurement interval, take
    the level of the rest: when its interval (alpha/2) excludes 1, or most windows are judged
    off 1 on the same side (family-wise), it is the systematic offset and the reference, else
    the reference is 1. When L - lambda W trends over the windows (a leak, a backlog building
    over the range), the reference is the offset's linear trend, not a constant. Repeat until
    the transient set is stable. A systematic offset needs most windows: with half or more of
    them transient there is none. The level is taken over the windows not flagged `not_steady`
    (L or lambda drifting significantly and materially inside the window) when they are most:
    L T = lambda W T holds over any window up to the edge term (requests in flight at its
    edges), and a backlog building or draining inside the window makes that term a real
    discrepancy of the process, not of the instruments, so such a window's R must not make a
    measurement-system offset (1i26). Otherwise (a leak, a long overload: not steady anywhere)
    all non-transient windows, and the trend takes the drift."""
    ws = out.windows
    ratio = {i: float(ws[i].ratio) for i in judged}  # type: ignore[arg-type]
    side = {v: sum(ws[i].verdict == v for i in judged) for v in ("L_high", "L_low")}
    majority = next((v for v, k in side.items() if 2 * k > len(judged)), None)
    drift = bool(out.growing and out.growing["significant"])
    steady = [i for i in judged if "not_steady" not in ws[i].flags]
    # the level's candidates: the steady windows when they are most (a leak or a long overload
    # may be not steady anywhere: then all of them, and the trend below takes the drift)
    pool = set(steady) if 2 * len(steady) > len(judged) else set(judged)
    med = float(np.median(list(ratio.values())))
    refs = dict.fromkeys(judged, med)
    ref_sd = ref_bias = 0.0
    trans: list[int] | None = None
    sysb: Block | None = None
    basis: list[int] = []
    for _ in range(MAX_ITER):
        new = [i for i in judged if _off(ws[i], refs[i], ref_sd, ref_bias, q)]
        core = [i for i in judged if i not in new and i in pool]
        # the level's windows: the non-transient ones when they are most; when most windows
        # are off 1 on one side the offset is persistent even if they differ among themselves
        basis = core if 2 * len(core) > len(judged) else (judged if majority else [])
        lvl = _level(sub, wins, ws, basis, alpha) if basis else None
        if lvl is not None and lvl.verdict == "consistent" and majority:
            lvl.verdict = majority  # persistent: most windows off 1 on one side
        sysb = lvl if lvl is not None and lvl.verdict in ("L_high", "L_low") else None
        if sysb is not None and sysb.ratio is not None and sysb.sd is not None:
            nsd, nb = sysb.sd, _bias(sysb)
            if drift and len(basis) >= 3:
                x = np.array(basis, float)
                slope, icpt = np.polyfit(x, [ratio[i] for i in basis], 1)
                nrefs = {i: float(icpt + slope * i) for i in judged}
            else:
                nrefs = dict.fromkeys(judged, sysb.ratio)
        else:
            nrefs, nsd, nb = dict.fromkeys(judged, 1.0), 0.0, 0.0
        done = new == trans and (nrefs, nsd, nb) == (refs, ref_sd, ref_bias)
        trans, refs, ref_sd, ref_bias = new, nrefs, nsd, nb
        if done:
            break
    # the final transient set is against the final reference
    final = [i for i in judged if _off(ws[i], refs[i], ref_sd, ref_bias, q)]
    out.reference = float(np.median(list(refs.values())))
    out.references = refs
    out.systematic = sysb
    out.basis = list(basis) if sysb is not None else []
    out.core = [i for i in judged if i not in final]
    # common-cause envelope: the small-system (Poisson) scale, or the process's own window-to-
    # window variation around the reference (SPC: 3 robust sigma of the windows, less what the
    # measurement interval already explains), whichever is wider
    dev = np.array([ratio[i] / refs[i] - 1 for i in judged], float)
    spread = 0.0
    few = dev.size < MIN_SPREAD
    if not few:
        mad_sd = 1.4826 * float(np.median(np.abs(dev - np.median(dev))))
        meas_sd = float(np.median([ws[i].sd / refs[i] for i in judged]))  # type: ignore[operator]
        spread = 3 * math.sqrt(max(0.0, mad_sd**2 - meas_sd**2))
    out.common_cause["spread_rel"] = spread
    # 4ahp: too few windows for the process's own variation: the special-cause label rests on
    # the cautious envelope (the window's own sub-step variation), the Poisson one alone gives
    # undetermined (principle 16)
    out.common_cause["envelope"] = "cautious" if few else "small_system_or_spread"
    if few:
        out.common_cause["cautious_rel95"] = float(
            np.median([ws[i].common_cautious for i in judged])  # type: ignore[misc]
        )
    lam = np.array([ws[i].lam for i in judged], float)
    W = np.array([ws[i].W_s for i in judged], float)
    lam_med, W_med = float(np.median(lam)), float(np.median(W))
    L_med = float(np.median([ws[i].L for i in judged]))
    lam_q75 = float(np.quantile(lam, 0.75))
    for i in judged:
        out.phases[i] = _load(ws[i], lam_med, lam_q75, W_med, L_med)
    for i in judged:
        w = ws[i]
        assert w.ratio is not None and w.common is not None
        if i not in final:
            w.source = MEASUREMENT
            continue
        # beyond the measurement interval: is it also beyond the common-cause envelope?
        d = abs(w.ratio / refs[i] - 1)
        env = max(w.common, spread)
        cautious = max(env, w.common_cautious or 0.0) if few else env
        w.source = cautious_label(d > cautious, d > env)
        t = _transient(i, w, refs[i], out.phases[i])
        t["envelope"] = {
            "deviation": d, "small_system": w.common, "spread": spread if not few else None,
            "cautious": cautious if few else None, "label_rests_on": "cautious" if few else
            "small_system_or_spread",
        }  # fmt: skip
        out.transient.append(t)


def _load(w: Block, lam_med: float, lam_q75: float, W_med: float, L_med: float) -> dict:
    """A window's load context: at a load peak (lambda or W well above the median window, or a
    backlog building), draining one, or neither."""
    assert w.lam is not None and w.W_s is not None and w.L is not None
    l0, l1 = w.L_edges or (w.L, w.L)
    backlog = l1 - l0
    noise = 3 * math.sqrt(2 * max(w.L, 1.0))  # Poisson-occupancy noise of the two edge readings
    lam_rel = w.lam / lam_med if lam_med > 0 else math.nan
    W_rel = w.W_s / W_med if W_med > 0 else math.nan
    peak = (lam_rel >= PEAK_LAMBDA and w.lam >= lam_q75) or W_rel >= PEAK_W
    building, draining = backlog > noise, backlog < -noise
    lam_up = bool((w.drift.get("lambda") or {}).get("significant")) and (
        w.drift["lambda"]["change"] > 0
    )
    phase = "drain" if draining and not building else "peak" if peak or building else "other"
    return {
        "phase": phase,
        "load": {
            "lambda_vs_median": lam_rel, "W_vs_median": W_rel,
            "L_vs_median": w.L / L_med if L_med > 0 else math.nan, "lambda_rising": lam_up,
            "L_start": l0, "L_end": l1, "backlog_change": backlog,
            "not_steady": "not_steady" in w.flags,
        },
    }  # fmt: skip


def _transient(i: int, w: Block, ref: float, ctx: dict) -> dict:
    """A transient window with its load context (its cause is worded after promotion)."""
    assert w.ratio is not None
    return {
        "index": i,
        "direction": "L_high" if w.ratio > ref else "L_low",
        "ratio": w.ratio,
        "vs_reference": w.ratio / ref,
        "source": w.source,
        "phase": ctx["phase"],
        "at_peak": ctx["phase"] == "peak",
        "load": dict(ctx["load"]),
        "promoted": False,
    }


#: a load-peak window inside the common-cause envelope, without evidence of leaving steady state
PEAK_COMMON = (
    "at a load peak; inside expected fluctuation — not a signal by itself; watch if it repeats "
    "or grows"
)


#: beyond the small-system (Poisson) envelope, inside the cautious one (fewer than MIN_SPREAD
#: windows: the process's own variation cannot be estimated)
PEAK_UNDETERMINED = (
    "at a load peak; beyond the small-system (Poisson) envelope but inside the cautious one (the "
    "window's own arrival bursts and latency variation): too few windows to estimate the "
    "process's own variation — undetermined; a longer range or a growing backlog would decide it"
)
UNDETERMINED_TEXT = (
    "beyond the small-system (Poisson) envelope but inside the cautious one (the window's own "
    "arrival bursts and latency variation; too few windows to estimate the process's own "
    "variation): undetermined"
)


def _cause(t: dict) -> str:
    if t["phase"] == "drain":
        return (
            "a backlog draining (after a peak): completions carry time spent before the window, "
            "so λW exceeds L; out of steady state"
        )
    if t["phase"] == "peak":
        if t["promoted"]:
            reason = t["promotion"]["reason"]
            return f"at a load peak, leaving steady state (promoted to special cause): {reason}"
        if t["source"] == UNDETERMINED:
            return PEAK_UNDETERMINED
        if t["source"] == SPECIAL:
            return (
                "at a load peak, beyond expected fluctuation: possible transition out of steady "
                "state (toward overload): a backlog building inside the window puts in-flight "
                "time in L that completed latencies (W) do not show yet"
            )
        return PEAK_COMMON
    if t["source"] == UNDETERMINED:
        return UNDETERMINED_TEXT
    return (
        "not at a load peak (lambda and W near their medians): a change confined to these "
        "windows — in-flight time the latency timer does not see (a queue excursion before "
        "it), a deploy, an instance joining or leaving, a routing or instrumentation change"
    )


# promotion of load-peak windows (83w, q2m) ----------------------------------------------------
def cantelli_k(alpha: float) -> float:
    """Cantelli's (one-sided Chebyshev) inequality P(X - EX >= k sd) <= 1 / (1 + k^2) holds for
    any distribution with that variance: the k it needs for a one-sided level alpha."""
    return math.sqrt(1 / alpha - 1)


def kendall_increasing_p(x: list[float]) -> tuple[int, float]:
    """Kendall's S of x against its order, and the exact one-sided p of an S this large under
    exchangeability (every order equally likely; heavy tails change nothing). Ties count
    against the trend: conservative."""
    n = len(x)
    inv = sum(1 for a in range(n) for b in range(a + 1, n) if not x[b] > x[a])
    counts = [1]  # Mahonian numbers: permutations of `size` items with j inversions
    for size in range(2, n + 1):
        new = [0] * (len(counts) + size - 1)
        for j, c in enumerate(counts):
            for add in range(size):
                new[j + add] += c
        counts = new
    return n * (n - 1) // 2 - 2 * inv, sum(counts[: inv + 1]) / math.factorial(n)


def _episodes(judged: list[int], phases: dict[int, dict]) -> list[list[int]]:
    """Runs of consecutive windows at a load peak or draining one: one load episode each."""
    out: list[list[int]] = []
    for i in judged:
        if phases[i]["phase"] not in ("peak", "drain"):
            continue
        if out and out[-1][-1] == i - 1:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def _dev(w: Block, ref: float) -> float:
    return abs(float(w.ratio) / ref - 1)  # type: ignore[arg-type]


def _backlog(
    sub: Substeps, idx: np.ndarray, w: Block, flow: float | None, use_flow: bool, var_n: float,
    k: float,
) -> dict:  # fmt: skip
    """(a) N(end) - N(start) over the window: the gauge's reading and, when the counter counts
    arrivals, arrivals - completions; steady state: mean 0, variance <= 2 var(N). With both,
    the smaller: the two instruments read the same backlog, and growth that only one of them
    sees is a measurement question (flow_imbalance), not the system leaving steady state."""
    before = idx[0] - 1
    ok0 = before >= 0 and bool(np.isfinite(sub.conc[before]))
    gauge = float(sub.conc[idx[-1]] - sub.conc[before if ok0 else idx[0]])
    x = min(gauge, flow) if use_flow and flow is not None else gauge
    scale = math.sqrt(2 * var_n)
    # the readings' own error: counts between a window edge and the nearest scrape (Poisson)
    T = w.n * sub.step_ms / 1000
    gap_s = (sub.scrape_ms or sub.step_ms) / 1000
    rates = float(w.lam or 0) + float(w.completions or 0) / max(T, 1e-9)
    half = 1.96 * math.sqrt(2 * rates * gap_s)
    reads = [gauge] + ([flow] if flow is not None else [])
    z = x / scale
    return {
        "kind": "backlog_growth", "value": x, "unit": "requests",
        "interval": (min(reads) - half, max(reads) + half),
        "gauge": gauge, "flow": flow, "flow_used": use_flow and flow is not None,
        "null_sd": scale, "z": z, "k": k, "p_bound": 1 / (1 + z * z) if z > 0 else 1.0,
        "significant": z >= k,
    }  # fmt: skip


def _w_noise(sub: Substeps, idx: np.ndarray) -> tuple[float, float]:
    """A window's mean latency: its standard error around the window's own linear trend (the
    sub-step means' residual sd over sqrt(n_eff), n_eff from the residuals' integrated
    autocorrelation time), never below the CV-1 sampling error W / sqrt(completions); and its df
    (n_eff - 2)."""
    s, c = sub.lat_sum[idx], sub.lat_count[idx]
    ok = np.isfinite(s) & np.isfinite(c) & (c > 0)
    if int(ok.sum()) < 5:
        return math.inf, 1.0
    w = s[ok] / c[ok]
    x = ((sub.ts_ms[idx][ok] - sub.ts_ms[idx][0]) // sub.step_ms).astype(np.int64)
    e = w - np.polyval(np.polyfit(x, w, 1), x)
    ne = n_eff(w.size, tau_int(x, e))
    W = float(s[ok].sum() / c[ok].sum())
    floor = W / math.sqrt(max(float(c[ok].sum()) * sub.step_ms / 1000, 1.0))
    return max(float(np.std(e, ddof=2)) / math.sqrt(ne), floor), max(ne - 2, 1.0)


def _latency_rise(
    i: int, ws: list[Block], noise: dict[int, tuple[float, float]], level: float
) -> dict | None:
    """(b) W rising across consecutive windows into the peak (i-2 -> i-1 -> i) or through it
    (i-1 -> i -> i+1): both rises beyond their own t threshold (Welch over the two windows'
    standard errors, df from n_eff) at `level`; the triple that clears its thresholds best."""
    best = None
    for which, tri in (("into", (i - 2, i - 1, i)), ("through", (i - 1, i, i + 1))):
        if not all(j in noise for j in tri):
            continue
        W = [float(ws[j].W_s) for j in tri]  # type: ignore[arg-type]
        zs, ks = [], []
        for a, b in ((0, 1), (1, 2)):
            (sa, da), (sb, db) = noise[tri[a]], noise[tri[b]]
            se = math.hypot(sa, sb)
            df = (sa**2 + sb**2) ** 2 / (sa**4 / da + sb**4 / db) if se > 0 else 1.0
            zs.append((W[b] - W[a]) / se if se > 0 else 0.0)
            ks.append(t_quantile(1 - level, df))
        score = min(z / k for z, k in zip(zs, ks, strict=True))
        if best is None or score > best[0]:
            best = (score, which, tri, W, zs, ks)
    if best is None:
        return None
    score, which, tri, W, zs, ks = best
    half = 1.96 * math.hypot(noise[tri[0]][0], noise[tri[2]][0])
    rise = W[2] - W[0]
    return {
        "kind": "latency_rise", "value": rise, "unit": "s",
        "interval": (rise - half, rise + half), "contrast": which, "windows": list(tri),
        "W": W, "z": zs, "k": ks, "level": level, "significant": score >= 1,
    }  # fmt: skip


def _promote(
    out: GroupResult, sub: Substeps, wins: list[np.ndarray], judged: list[int], tests: int,
    arrivals: str,
) -> None:  # fmt: skip
    """Option C (83w; user decision 2026-10-03). A window at a load peak whose deviation is inside
    the common-cause envelope (or the measurement interval) keeps that label by default; it is
    PROMOTED to special cause when independent evidence says the system is leaving steady state:

    (a) the backlog growing over the window (the gauge; when the counter counts arrivals also
        arrivals - completions, the flow balance of q2m, and the smaller of the two). Steady state: mean 0, variance <= 2 var(N), var(N) from
        the gauge in the windows outside the peak's episode (and its neighbours), floored at
        their mean (Poisson occupancy).
    (b) W rising across consecutive windows into or through the peak: two successive rises
        (i-2 -> i-1 -> i or i-1 -> i -> i+1), each beyond a t threshold on the two windows'
        standard errors around their own within-window trend (n_eff df). A step to a higher
        but steady load raises W once, then holds: one rise is not enough.
    (c) the deviation |L / (lambda W) / reference - 1| growing across repeated load episodes:
        Kendall's S over the episodes' largest peak deviations, exact under exchangeability.

    None of them uses the deviation's envelope. Multiplicity: PROMOTE_ALPHA family-wise per
    check, a third per evidence type: (a) Bonferroni over `tests` (windows x groups) at
    Cantelli's distribution-free bound (queue excursions are heavy-tailed); (b) Bonferroni over
    `tests` x 2 triples, both rises required (the intersection's level is at most either's);
    (c) once per group (Bonferroni over groups)."""
    ws = out.windows
    phases = out.phases
    a_each = PROMOTE_ALPHA / 3
    k_a, level_b = cantelli_k(a_each / tests), a_each / (2 * tests)
    a_c = a_each / max(1, round(tests / max(1, len(wins))))
    out.promotion = {
        "alpha": PROMOTE_ALPHA, "tests": tests, "k_backlog": k_a, "level_latency": level_b,
        "alpha_peaks": a_c,
    }  # fmt: skip
    episodes = _episodes(judged, phases)
    episode_of = {i: ep for ep in episodes for i in ep}
    usable = sub.usable()
    # flow balance: arrivals - completions, the counter scaled by the windows' median
    # counter / count ratio (a counter counting a subset or superset is not a backlog)
    A = {i: float(ws[i].arrivals or 0) for i in judged}
    D = {i: float(ws[i].completions or 0) for i in judged}
    same = all(abs(A[i] - D[i]) <= 1e-9 * max(A[i], 1.0) for i in judged)
    r_hat = float(np.median([A[i] / D[i] for i in judged if D[i] > 0] or [1.0]))
    use_flow = arrivals == "arrivals" and not same
    noise = {j: _w_noise(sub, wins[j][usable[wins[j]]]) for j in judged}
    evidence: dict[int, list[dict]] = {}
    for i in judged:
        if phases[i]["phase"] != "peak":
            continue
        ep = episode_of[i]
        base = [j for j in judged if not ep[0] - 1 <= j <= ep[-1] + 1]
        if len(base) < MIN_BASELINE:
            continue
        g = np.concatenate([sub.conc[wins[j][usable[wins[j]]]] for j in base])
        var_n = max(float(np.var(g, ddof=1)), float(np.mean(g)), 1e-12)
        flow = None if same else A[i] - r_hat * D[i]
        idx = wins[i][usable[wins[i]]]
        ev = [_backlog(sub, idx, ws[i], flow, use_flow, var_n, k_a)]
        lr = _latency_rise(i, ws, noise, level_b)
        ev += [lr] if lr else []
        for e in ev:
            e["baseline_windows"] = len(base)
        evidence[i] = ev
    # (c) repeated load episodes: each one's largest peak deviation, in time order
    reps = []
    for ep in episodes:
        peaks = [i for i in ep if phases[i]["phase"] == "peak"]
        if peaks:
            reps.append(max(peaks, key=lambda i: _dev(ws[i], out.references[i])))
    if len(reps) >= 3:
        devs = [_dev(ws[i], out.references[i]) for i in reps]
        s, p = kendall_increasing_p(devs)
        out.promotion["peaks"] = {"windows": reps, "deviations": devs, "kendall_s": s, "p": p}
        sd = lambda i: float(ws[i].sd or 0) / out.references[i]
        for i, d in zip(reps[1:], devs[1:], strict=True):
            half = 1.96 * math.hypot(sd(reps[0]), sd(i))
            evidence.setdefault(i, []).append({
                "kind": "peak_growth", "value": d - devs[0], "unit": "relative",
                "interval": (d - devs[0] - half, d - devs[0] + half),
                "first_peak": reps[0], "deviation_first": devs[0], "deviation": d,
                "peaks": len(reps), "kendall_s": s, "p": p, "alpha": a_c,
                "significant": p <= a_c and d > devs[0],
            })  # fmt: skip
    for i in sorted(evidence):
        w, ev = ws[i], evidence[i]
        hits = [e for e in ev if e["significant"]]
        if not hits or w.source not in (COMMON, MEASUREMENT, UNDETERMINED):
            continue
        prom = {"from": w.source, "evidence": ev, "reason": "; ".join(_reason(e) for e in hits)}
        w.promotion = prom
        out.promoted.append({
            "index": i, "from": w.source,
            "deviation": {COMMON: "within_envelope", UNDETERMINED: "beyond_small_system_only"}.get(
                w.source, "within_measurement"
            ),
            "evidence": ev, "reason": prom["reason"], "load": phases[i]["load"],
        })  # fmt: skip
        w.source = SPECIAL
        for t in out.transient:
            if t["index"] == i:
                t.update(source=SPECIAL, promoted=True, promotion=prom)


def _reason(e: dict) -> str:
    """Why a window was promoted, with the numbers."""
    f = lambda v: f"{v:+.3g}"
    if e["kind"] == "backlog_growth":
        src = f"gauge {f(e['gauge'])}" + (
            f", arrivals − completions {f(e['flow'])}" if e["flow"] is not None else ""
        )
        return (
            f"backlog grew {f(e['value'])} requests in the window ({src}): {e['z']:.3g}× the "
            f"steady-state scale √2·σ_N = {e['null_sd']:.3g} (threshold {e['k']:.3g}; Cantelli "
            f"p ≤ {e['p_bound']:.2g})"
        )
    if e["kind"] == "latency_rise":
        path = " → ".join(f"{w:.3g} s" for w in e["W"])
        zs = ", ".join(f"{z:.3g}" for z in e["z"])
        ks = ", ".join(f"{k:.3g}" for k in e["k"])
        return (
            f"mean latency W rose across consecutive windows {e['contrast']} the peak: {path} "
            f"({f(e['value'])} s; each rise z = {zs} ≥ t thresholds {ks})"
        )
    return (
        f"the deviation grows across {e['peaks']} repeated load peaks: |L ÷ λW vs reference − 1| "
        f"{100 * e['deviation_first']:.0f}% at the first → {100 * e['deviation']:.0f}% here "
        f"(Kendall S = {e['kendall_s']}, exact p = {e['p']:.2g} ≤ {e['alpha']:.2g})"
    )


def _common_cause(out: GroupResult, judged: list[int]) -> None:
    """The small-system envelope: how much a window's L and lambda W fluctuate at this traffic."""
    ws = out.windows
    rel = float(np.median([ws[i].common for i in judged]))
    n = float(np.median([ws[i].completions for i in judged]))
    inside = [t["index"] for t in out.transient if t["source"] == COMMON]
    warn = None
    if rel >= COMMON_CAUSE_WARN or inside:
        warn = (
            f"at this traffic (N≈{n:.0f} completions per window) L and λW legitimately "
            f"fluctuate ±{100 * rel:.0f}% per window (common cause: a small system's aggregates "
            "do not average out); window differences smaller than that are not distinguishable "
            "from small-system behaviour"
            + (f" ({len(inside)} transient window(s) are within it)" if inside else "")
        )
    out.common_cause.update({
        "completions_per_window": n,
        "rel95": rel,
        "pooled_rel95": out.pooled.common,
        "warning": warn,
    })  # fmt: skip


def _growing(windows: list[Block]) -> dict | None:
    """Trend of L - lambda W across windows: a leak or a backlog grows."""
    pts = [
        (w.end_ms, w.L - w.lambda_W) for w in windows if w.L is not None and w.lambda_W is not None
    ]
    if len(pts) < 6:
        return None
    ts = np.array([p[0] for p in pts], np.int64)
    y = np.array([p[1] for p in pts])
    if float(np.std(y)) == 0:
        return None
    step = int(np.min(np.diff(ts))) or 1
    pos = ((ts - ts[0]) // step).astype(np.int64)
    tr = trend(pos, ts, y, int(ts[-1] - ts[0]))
    return {
        "slope_per_h": tr.slope_per_h,
        "slope_ci": tr.interval_per_h,
        "significant": tr.significant,
        "growing": tr.significant and tr.interval_per_h[0] > 0,
    }


def combine(groups: list[Substeps]) -> Substeps:
    """The total over groups (what an ungrouped check sees): sums per sub-step on the union grid,
    each quantity over the groups that reported it."""
    ts = np.unique(np.concatenate([g.ts_ms for g in groups]))

    def total(attr: str, op=np.add) -> np.ndarray:
        out = np.zeros(ts.size)
        seen = np.zeros(ts.size, bool)
        for g in groups:
            at = np.searchsorted(ts, g.ts_ms)
            v = getattr(g, attr)
            ok = np.isfinite(v)
            op.at(out, at[ok], v[ok])
            seen[at[ok]] = True
        return np.where(seen, out, np.nan)

    first = groups[0]
    return Substeps(
        ts, total("arrivals"), total("lat_sum"), total("lat_count"), total("conc"),
        # the summed gauge is sampled at the same scrapes: its samples are not added up
        np.nan_to_num(total("conc_n", np.maximum)), first.step_ms, first.lookback_ms,
        first.scrape_ms,
    )  # fmt: skip
