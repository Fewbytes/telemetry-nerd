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
- special cause: TRANSIENT windows beyond both, against the systematic level (load peaks, leaving
  steady state, a change in the instrumentation).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from telemetry_nerd.analysis.sources import COMMON, MEASUREMENT, SPECIAL
from telemetry_nerd.analysis.stability import trend
from telemetry_nerd.analysis.stats import t_quantile

ALPHA = 0.05
MIN_SUBSTEPS = 4  # fewer usable sub-steps: the window is not judged
STEADY_CHANGE = 0.10  # a significant trend moving L or lambda by more than this is "not steady"
COMMON_CAUSE_WARN = 0.10  # common-cause half-width (95%, relative) from which the warning is raised
PEAK_LAMBDA = 1.1  # a window's lambda this far above the median window: a load peak
PEAK_W = 1.5  # a window's W this far above the median window: a latency surge
MAX_ITER = 6


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
    source: str | None = None  # deviation from the reference: MEASUREMENT | COMMON | SPECIAL


@dataclass
class GroupResult:
    windows: list[Block]
    pooled: Block  # the whole range
    verdict: str  # consistent | L_high | L_low | inconsistent_in_windows | insufficient
    flagged: list[int]  # windows whose measurement interval (family-wise) excludes 1
    growing: dict | None = None  # trend of L - lambda W over windows (leak / backlog)
    reference: float = 1.0  # the level windows are compared with: 1, or the systematic offset
    references: dict[int, float] = field(default_factory=dict)  # per window (a drifting offset)
    systematic: Block | None = None  # pooled over the non-transient windows, when it excludes 1
    core: list[int] = field(default_factory=list)  # the windows of that pool
    transient: list[dict] = field(default_factory=list)
    common_cause: dict = field(default_factory=dict)


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
    gap_s = (sub.scrape_ms or sub.step_ms) / 1000
    r_lam = math.sqrt(2 * segs * lam * gap_s) / max(lam * T, 1e-12)
    r_w = math.sqrt(2 * segs * C * gap_s) / max(C * T, 1e-12)
    sd_count = R * (r_lam + r_w)
    sd = math.sqrt((sd_L / lw) ** 2 + sd_edge**2 + sd_count**2)
    blk.sd, blk.sd_source = sd, "empirical" if var_emp >= var_floor else "floor"
    blk.sd_terms = {
        "gauge_sampling": sd_L / lw, "edge_straddle": sd_edge, "scrape_timing": sd_count,
    }  # fmt: skip
    # 4. rate() lookback (a bias bound: added to the half-width, never in quadrature): each
    # sub-step's rate() looks back delta further than the gauge average, so the window's counts
    # are those of a window shifted by delta: off by delta x (rate at the start - rate at the
    # end), each end's rate from the sub-steps one rate interval covers, plus that estimate's
    # own counting noise
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
    qt = q95 if q_test is None else t_quantile(q_test, df)
    blk.ci95 = (max(0.0, R - q95 * sd - align), R + q95 * sd + align)
    blk.ci_test = (max(0.0, R - qt * sd - align), R + qt * sd + align)
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
    blk.common = q95 * (1 / math.sqrt(max(lam * T, 1.0)) + 1 / math.sqrt(max(C * T, 1.0)))

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
    sub: Substeps, k: int, skip: int = 0, tests: int | None = None, alpha: float = ALPHA
) -> GroupResult:
    """Windows of k sub-steps and the pooled span after `skip` sub-steps of warm-up.

    alpha is split: alpha/2 for the systematic offset (pooled over the windows that are not
    transient), alpha/2 family-wise (Bonferroni over `tests`, default this group's windows) for
    the window tests (against 1 and against the systematic level)."""
    wins = window_indexes(sub, k, skip)
    m = tests or max(1, len(wins))
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
    if out.systematic is not None:
        out.verdict = out.systematic.verdict
    elif out.transient:
        out.verdict = "inconsistent_in_windows"
    else:
        out.verdict = "consistent"
    _common_cause(out, judged)
    return out


def _off(w: Block, ref: float, ref_sd: float, ref_bias: float, q: float) -> bool:
    """Does window w differ from the reference level beyond the measurement interval?"""
    assert w.ratio is not None and w.sd is not None
    half = t_quantile(q, max(w.n - 1, 1)) * math.hypot(w.sd, ref_sd)
    return abs(w.ratio - ref) > half + w.bias.get("alignment", 0.0) + ref_bias


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
    align = float(np.mean([w.bias.get("alignment", 0.0) for w in b]))
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
    them transient there is none."""
    ws = out.windows
    ratio = {i: float(ws[i].ratio) for i in judged}  # type: ignore[arg-type]
    side = {v: sum(ws[i].verdict == v for i in judged) for v in ("L_high", "L_low")}
    majority = next((v for v, k in side.items() if 2 * k > len(judged)), None)
    drift = bool(out.growing and out.growing["significant"])
    med = float(np.median(list(ratio.values())))
    refs = dict.fromkeys(judged, med)
    ref_sd = ref_bias = 0.0
    trans: list[int] | None = None
    sysb: Block | None = None
    for _ in range(MAX_ITER):
        new = [i for i in judged if _off(ws[i], refs[i], ref_sd, ref_bias, q)]
        core = [i for i in judged if i not in new]
        # the level's windows: the non-transient ones when they are most; when most windows
        # are off 1 on one side the offset is persistent even if they differ among themselves
        basis = core if 2 * len(core) > len(judged) else (judged if majority else [])
        lvl = _level(sub, wins, ws, basis, alpha) if basis else None
        if lvl is not None and lvl.verdict == "consistent" and majority:
            lvl.verdict = majority  # persistent: most windows off 1 on one side
        sysb = lvl if lvl is not None and lvl.verdict in ("L_high", "L_low") else None
        if sysb is not None and sysb.ratio is not None and sysb.sd is not None:
            nsd, nb = sysb.sd, sysb.bias.get("alignment", 0.0)
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
    out.core = [i for i in judged if i not in final]
    # common-cause envelope: the small-system (Poisson) scale, or the process's own window-to-
    # window variation around the reference (SPC: 3 robust sigma of the windows, less what the
    # measurement interval already explains), whichever is wider
    dev = np.array([ratio[i] / refs[i] - 1 for i in judged], float)
    spread = 0.0
    if dev.size >= 5:
        mad_sd = 1.4826 * float(np.median(np.abs(dev - np.median(dev))))
        meas_sd = float(np.median([ws[i].sd / refs[i] for i in judged]))  # type: ignore[operator]
        spread = 3 * math.sqrt(max(0.0, mad_sd**2 - meas_sd**2))
    out.common_cause["spread_rel"] = spread
    lam = np.array([ws[i].lam for i in judged], float)
    W = np.array([ws[i].W_s for i in judged], float)
    lam_med, W_med = float(np.median(lam)), float(np.median(W))
    L_med = float(np.median([ws[i].L for i in judged]))
    lam_q75 = float(np.quantile(lam, 0.75))
    for i in judged:
        w = ws[i]
        assert w.ratio is not None and w.common is not None
        if i not in final:
            w.source = MEASUREMENT
            continue
        # beyond the measurement interval: is it also beyond the common-cause envelope?
        w.source = SPECIAL if abs(w.ratio / refs[i] - 1) > max(w.common, spread) else COMMON
        out.transient.append(_transient(i, w, refs[i], lam_med, lam_q75, W_med, L_med))


def _transient(
    i: int, w: Block, ref: float, lam_med: float, lam_q75: float, W_med: float, L_med: float
) -> dict:
    """A transient window with its load context: is it at a load peak, leaving steady state?"""
    assert w.ratio is not None and w.lam is not None and w.W_s is not None and w.L is not None
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
    if draining and not building:
        phase = "drain"
        cause = (
            "a backlog draining (after a peak): completions carry time spent before the window, "
            "so λW exceeds L; out of steady state"
        )
    elif peak or building:
        phase = "peak"
        cause = (
            "at a load peak: possible transition out of steady state (toward overload): "
            "a backlog building inside the window puts in-flight time in L that completed "
            "latencies (W) do not show yet"
        )
    else:
        phase = "other"
        cause = (
            "not at a load peak (lambda and W near their medians): a change confined to these "
            "windows — in-flight time the latency timer does not see (a queue excursion before "
            "it), a deploy, an instance joining or leaving, a routing or instrumentation change"
        )
    return {
        "index": i,
        "direction": "L_high" if w.ratio > ref else "L_low",
        "ratio": w.ratio,
        "vs_reference": w.ratio / ref,
        "source": w.source,
        "phase": phase,
        "at_peak": phase == "peak",
        "load": {
            "lambda_vs_median": lam_rel, "W_vs_median": W_rel,
            "L_vs_median": w.L / L_med if L_med > 0 else math.nan, "lambda_rising": lam_up,
            "L_start": l0, "L_end": l1, "backlog_change": backlog,
            "not_steady": "not_steady" in w.flags,
        },
        "cause": cause,
    }  # fmt: skip


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
