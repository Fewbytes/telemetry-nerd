"""Seasonal comparison for latency (bead lkn.7): the histogram per cycle, never percentiles.

Each cycle (now and the same window on previous days / weeks) is a window histogram: bucket
counts summed over the window (counts are additive; percentiles are not, so no percentile is
ever pooled or averaged across cycles). Per cycle we read the share of observations above a
threshold at a bucket edge (exact, fraction_over) and the shape distance to the other cycles.
The band for the share comes from the spread of the cycles' shares (on the logit scale, Student
t with k-1 df, floored at binomial sampling noise), like the level detector of the time-series
comparison. Pure numpy, no I/O. Design: docs/superpowers/specs/2026-10-02-seasonal-compare-design.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from telemetry_nerd.analysis.seasonal import (
    ALPHA,
    CHOICE_GAIN,
    MIN_COVERAGE,
    MIN_CYCLES,
    SCHEMES,
    atypical_levels,
    t_quantile,
)

TARGET_SHARE = 0.01  # default threshold: the reference's ~p99 bucket edge
MIN_OVER = 20  # expected observations above the threshold per cycle for a stable share


@dataclass
class Hist:
    """One cycle's window histogram: counts per (lo, hi] bucket, summed over the window."""

    j: int  # 0 = now
    start_ms: int
    end_ms: int
    lo: np.ndarray
    hi: np.ndarray
    c: np.ndarray
    coverage: float = 1.0  # share of the window's steps with data
    dataset: str | None = None

    @property
    def n(self) -> float:
        return float(np.sum(self.c))

    def over(self, x: float) -> float:
        """Count above the edge x (x must be an edge of this histogram's buckets)."""
        return float(np.sum(self.c[self.lo >= x]))

    def cdf(self, edges: np.ndarray) -> np.ndarray:
        n = self.n
        if n <= 0:
            return np.full(edges.size, np.nan)
        return np.array([float(np.sum(self.c[self.hi <= e])) / n for e in edges])


def _exact_at(h: Hist, x: float) -> bool:
    """x splits h's counts exactly: no populated bucket strictly contains it."""
    return not bool(np.any((h.lo < x) & (h.hi > x) & (h.c > 0)))


def common_edges(hists: list[Hist]) -> np.ndarray:
    """Finite bucket edges at which every histogram with data splits exactly (empty buckets are
    absent from the rows, so an edge next to them is still exact)."""
    hs = [h for h in hists if h.n > 0]
    if not hs:
        return np.array([])
    cand = {float(v) for h in hs for v in np.concatenate([h.lo, h.hi]) if math.isfinite(v)}
    return np.array(sorted(e for e in cand if all(_exact_at(h, e) for h in hs)))


def snap(x: float, edges: np.ndarray) -> float | None:
    """The common edge nearest x (log distance)."""
    ok = [float(e) for e in edges if e > 0]
    if not ok:
        return None
    return min(ok, key=lambda e: abs(math.log(e / x)) if x > 0 else abs(e - x))


def default_threshold(refs: list[Hist], edges: np.ndarray) -> tuple[float | None, bool]:
    """From the reference cycles only (never from now): the edge whose pooled share above is
    nearest 1% (log distance) among edges with >= MIN_OVER expected per cycle above; else the
    nearest-1% edge with few counts above (second value False)."""
    refs = [h for h in refs if h.n > 0]
    if not refs or edges.size == 0:
        return None, False
    n = sum(h.n for h in refs)
    cands = []
    for e in edges:
        a = sum(h.over(e) for h in refs)
        if 0 < a < n:
            cands.append((abs(math.log((a / n) / TARGET_SHARE)), a / len(refs) >= MIN_OVER, e))
    if not cands:
        return None, False
    enough = [c for c in cands if c[1]]
    pick = min(enough or cands)
    return float(pick[2]), bool(enough)


def logit_share(over: float, n: float) -> tuple[float, float]:
    """Empirical logit of the share above (continuity-corrected) and its binomial variance."""
    p = (over + 0.5) / (n + 1)
    return math.log(p / (1 - p)), 1 / ((n + 1) * p * (1 - p))


def pct(v: float) -> str:
    return f"{100 * v:.3g}%"


def _expit(v: float) -> float:
    return 1 / (1 + math.exp(-v))


def distance(a: Hist, b_counts: tuple[np.ndarray, np.ndarray, np.ndarray], edges) -> float:
    """Largest |F_a - F_b| over the common edges (exact there: no interpolation)."""
    lo, hi, c = b_counts
    b = Hist(-1, 0, 0, lo, hi, c)
    if a.n <= 0 or b.n <= 0 or edges.size == 0:
        return math.nan
    return float(np.max(np.abs(a.cdf(edges) - b.cdf(edges))))


def _pool(hists: list[Hist]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sum counts per bucket (additive)."""
    acc: dict[tuple[float, float], float] = {}
    for h in hists:
        for lo, hi, c in zip(h.lo, h.hi, h.c, strict=True):
            acc[(float(lo), float(hi))] = acc.get((float(lo), float(hi)), 0.0) + float(c)
    keys = sorted(acc, key=lambda k: (k[1], k[0]))
    return (
        np.array([k[0] for k in keys]),
        np.array([k[1] for k in keys]),
        np.array([acc[k] for k in keys]),
    )


@dataclass(frozen=True)
class Share:
    value: float  # now's share above x
    centre: float  # expit(mean logit share of the kept cycles)
    normal: tuple[float, float]  # 90% prediction interval for a normal cycle's share
    p_interval: tuple[float, float]  # 99%
    previous: list[float]  # each kept cycle's share
    flagged: bool


@dataclass
class TailComparison:
    scheme: str
    verdict: str  # insufficient_history | unusual | usual
    reasons: list[str]
    threshold: float | None
    now: Hist
    kept: list[Hist] = field(default_factory=list)
    excluded: list[tuple[Hist, str]] = field(default_factory=list)
    direction: str | None = None
    share: Share | None = None
    shape: float = math.nan  # now vs the pooled kept cycles
    shape_previous: list[float] = field(default_factory=list)  # each kept cycle vs the others
    score: float = math.nan
    caveats: list[str] = field(default_factory=list)


def compare_tail(
    now: Hist,
    cycles: list[Hist],
    x: float | None,
    scheme: str,
    exclude: set[int] | frozenset[int] = frozenset(),
) -> TailComparison:
    """Now's share above x against the spread of the previous cycles' shares."""
    out = TailComparison(scheme, "insufficient_history", [], x, now)
    kept: list[Hist] = []
    for h in cycles:
        if h.j in exclude:
            out.excluded.append((h, "user"))
        elif h.n <= 0 or h.coverage < MIN_COVERAGE:
            out.excluded.append((h, "missing"))
        else:
            kept.append(h)
    if out.excluded:
        out.caveats.append("cycles_excluded")
    out.kept = kept
    if x is None:
        out.reasons.append("no bucket edge shared by now and the previous cycles")
        return out
    if len(kept) < MIN_CYCLES:
        out.reasons.append(
            f"{len(kept)} usable previous {scheme} cycle{'s' if len(kept) != 1 else ''} "
            f"(< {MIN_CYCLES}); {len(cycles)} looked at"
        )
        return out
    if now.n <= 0:
        out.reasons.append("no observations in the window")
        return out

    lv = [logit_share(h.over(x), h.n) for h in kept]
    L = np.array([a for a, _ in lv])
    V = np.array([v for _, v in lv])
    out.score = (
        float(np.mean([abs(L[j] - np.mean(np.delete(L, j))) for j in range(len(L))]))
        if len(L) > 1
        else math.nan
    )
    drop = atypical_levels(L, math.sqrt(float(np.mean(V))))
    if drop:
        for i in drop:
            out.excluded.append((kept[i], "atypical"))
        kept = [h for i, h in enumerate(kept) if i not in drop]
        L, V = np.delete(L, drop), np.delete(V, drop)
        if "cycles_excluded" not in out.caveats:
            out.caveats.append("cycles_excluded")
    out.kept = kept
    k = len(kept)
    l0, v0 = logit_share(now.over(x), now.n)
    m = float(np.mean(L))
    # cycle-to-cycle variance, never below the cycles' own sampling noise; now's sampling noise
    # beyond theirs (fewer observations now) adds to the prediction variance
    s2 = max(float(np.var(L, ddof=1)), float(np.mean(V)))
    var = s2 * (1 + 1 / k) + max(0.0, v0 - float(np.mean(V)))
    sd = math.sqrt(var)
    h90 = t_quantile(0.95, k - 1) * sd
    h99 = t_quantile(1 - ALPHA / 2, k - 1) * sd
    flagged = not m - h99 <= l0 <= m + h99
    out.share = Share(
        now.over(x) / now.n, _expit(m), (_expit(m - h90), _expit(m + h90)),
        (_expit(m - h99), _expit(m + h99)), [h.over(x) / h.n for h in kept], flagged,
    )  # fmt: skip
    if min(h.over(x) for h in kept) < MIN_OVER:
        out.caveats.append("few_over_threshold")

    edges = common_edges([now, *kept])
    pooled = _pool(kept)
    out.shape = distance(now, pooled, edges)
    out.shape_previous = [distance(h, _pool([g for g in kept if g is not h]), edges) for h in kept]

    sh = out.share
    if flagged:
        out.verdict = "unusual"
        out.direction = "higher" if l0 > m else "lower"
        out.reasons.append(
            f"{pct(sh.value)} of {now.n:.0f} observations above {x:g} vs "
            f"{pct(sh.normal[0])}..{pct(sh.normal[1])} for a normal cycle (90%, from the "
            f"spread of {k} cycles: {', '.join(pct(v) for v in sh.previous)})"
        )
    else:
        out.verdict = "usual"
        out.reasons.append(
            f"within normal: {pct(sh.value)} above {x:g} (normal {pct(sh.normal[0])}.."
            f"{pct(sh.normal[1])}, {k} cycles)"
        )
    return out


def choose_tail(
    now: Hist,
    schemes: dict[str, list[Hist]],
    x: float | None,
    exclude: dict[str, set[int]] | None = None,
) -> tuple[str | None, dict[str, float]]:
    """Pick the reference by leave-one-cycle-out error of the logit share (before exclusion), in
    SCHEMES order: a more specific scheme must beat the current pick by >= 5%."""
    scores: dict[str, float] = {}
    pick = None
    for s in SCHEMES:
        if not schemes.get(s):
            continue
        c = compare_tail(now, schemes[s], x, s, (exclude or {}).get(s, frozenset()))
        if c.verdict == "insufficient_history" or not math.isfinite(c.score):
            continue
        scores[s] = c.score
        if pick is None or c.score < CHOICE_GAIN * scores[pick]:
            pick = s
    return pick, scores
