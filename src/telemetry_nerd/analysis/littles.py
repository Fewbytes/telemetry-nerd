"""Little's law check: measured mean concurrency L against throughput x mean latency (czt.2).

Pure numpy over aligned sub-steps. Design: docs/superpowers/specs/2026-10-02-littles-law-design.md.

R = L / (lambda * W) per window. The interval is the delta method over per-sub-step residuals of
all four measured quantities (empirical covariance, Geyer tau for autocorrelation), never
narrower than worst-case floors for what the sub-steps cannot show (gauge sampling, Poisson
counts), widened by bias bounds for window edges and rate-window alignment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from telemetry_nerd.analysis.stability import trend
from telemetry_nerd.analysis.stats import t_quantile

ALPHA = 0.05
MIN_SUBSTEPS = 4  # fewer usable sub-steps: the window is not judged
STEADY_CHANGE = 0.10  # a significant trend moving L or lambda by more than this is "not steady"
FLOW_TOLERANCE = 0.0  # flow imbalance is judged by its interval alone


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

    def usable(self) -> np.ndarray:
        return (
            np.isfinite(self.arrivals)
            & np.isfinite(self.lat_sum)
            & np.isfinite(self.lat_count)
            & np.isfinite(self.conc)
        )


@dataclass
class Block:
    """One judged span (a window, or everything pooled)."""

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
    ci95: tuple[float, float] | None = None
    ci_test: tuple[float, float] | None = None
    sd: float | None = None
    sd_source: str | None = None  # empirical | floor: which bounds the gauge sampling error
    sd_terms: dict = field(default_factory=dict)  # each component's sd, in ratio units
    bias: dict = field(default_factory=dict)
    arrivals: float | None = None  # count in the span
    completions: float | None = None
    gauge_samples: int | None = None
    flags: list[str] = field(default_factory=list)
    drift: dict = field(default_factory=dict)
    flow: dict | None = None
    reason: str | None = None


@dataclass
class GroupResult:
    windows: list[Block]
    pooled: Block
    verdict: str  # consistent | L_high | L_low | inconsistent_in_windows | insufficient
    flagged: list[int]  # indexes of windows with an L_high / L_low verdict
    growing: dict | None = None  # trend of L - lambda W over windows (leak / backlog)


def _t(p: float, df: float) -> float:
    return t_quantile(p, max(df, 1.0))


def _systematic(dt_s: float, w_s: float) -> float:
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
    if lam <= 0 or C <= 0 or S <= 0:
        blk.verdict = "no_traffic"
        blk.reason = "no arrivals or completions in the window: lambda W is 0"
        if L > 0:
            blk.flags.append("in_flight_without_traffic")
        return blk
    W = S / C
    lw = lam * W
    R = L / lw
    blk.W_s, blk.lambda_W, blk.ratio = W, lw, R

    # 1. L: the gauge's time average from m scrape samples vs the path's true time average.
    # Successive differences estimate the sampling error from the data (a fast process: the
    # sample variance / m; a slow one: small, as it should be); the floor is the error a
    # Poisson-occupancy process with correlation time W would have under this sampling, i.e.
    # what scrapes can miss between them (short spikes) even when every sample looks alike.
    m = max(samples, n)
    dg = np.diff(g)
    var_emp = float(dg @ dg) / (2 * max(dg.size, 1)) / n if dg.size else math.inf
    var_floor = max(L, lw) * _systematic(sub.step_ms / 1000 * n / m, W) / m
    sd_L = math.sqrt(max(var_emp, var_floor))
    # 2. lambda and W: Poisson on the counts (relative 1/sqrt(N)); their correlation is unknown,
    # so they add linearly (worst case); L's error comes from another instrument (sampling
    # instants), independent of the counting: in quadrature with it (delta method on log R).
    r_lam = 1 / math.sqrt(max(lam * T, 1.0))
    r_w = 1 / math.sqrt(max(C * T, 1.0))
    sd = math.sqrt((sd_L / lw) ** 2 + (R * (r_lam + r_w)) ** 2)
    blk.sd, blk.sd_source = sd, "empirical" if var_emp >= var_floor else "floor"
    blk.sd_terms = {"L": sd_L / lw, "arrivals": R * r_lam, "latency": R * r_w}
    # 3. bias bounds (added to the half-width, never in quadrature)
    w_sub = np.where(c > 0, s / np.where(c > 0, c, 1.0), np.nan)
    w_max = float(np.nanmax(w_sub)) if np.isfinite(w_sub).any() else W
    edge = float(g[0] + g[-1]) * w_max / (lw * T)
    delta_s = sub.lookback_ms / 1000

    def rel_range(x: np.ndarray, mean: float) -> float:
        return float(np.ptp(x)) / mean if mean > 0 else 0.0

    align = R * (delta_s / T) * max(rel_range(a, lam), rel_range(s, S), rel_range(c, C))
    blk.bias = {"edge": edge, "alignment": align}
    bias = edge + align
    df = max(n - 1, 1)
    q95 = _t(1 - alpha / 2, df)
    qt = q95 if q_test is None else _t(q_test, df)
    blk.ci95 = (max(0.0, R - q95 * sd - bias), R + q95 * sd + bias)
    blk.ci_test = (max(0.0, R - qt * sd - bias), R + qt * sd + bias)
    if blk.ci_test[0] > 1:
        blk.verdict = "L_high"
    elif blk.ci_test[1] < 1:
        blk.verdict = "L_low"
    else:
        blk.verdict = "consistent"
    # per-side intervals (panel bands)
    half_L = q95 * sd_L + edge * lw
    blk.L_ci = (max(0.0, L - half_L), L + half_L)
    half_lw = lw * (q95 * (r_lam + r_w)) + align * lw / max(R, 1e-12)
    blk.lambda_W_ci = (max(0.0, lw - half_lw), lw + half_lw)

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

    alpha is split: alpha/2 for the pooled verdict, alpha/2 family-wise (Bonferroni over `tests`,
    default this group's windows) for the window verdicts."""
    wins = window_indexes(sub, k, skip)
    m = tests or max(1, len(wins))
    q_win = 1 - alpha / (4 * m)
    windows = [judge(sub, w, q_win, alpha) for w in wins]
    allidx = np.arange(skip, sub.ts_ms.size)
    pooled = (
        judge(sub, allidx, 1 - alpha / 4, alpha) if allidx.size else Block(0, 0, 0, "insufficient")
    )
    flagged = [i for i, w in enumerate(windows) if w.verdict in ("L_high", "L_low")]
    if pooled.verdict in ("L_high", "L_low"):
        verdict = pooled.verdict
    elif flagged:
        verdict = "inconsistent_in_windows"
    elif pooled.verdict == "consistent":
        verdict = "consistent"
    else:
        verdict = pooled.verdict
    return GroupResult(windows, pooled, verdict, flagged, _growing(windows))


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
    )  # fmt: skip
