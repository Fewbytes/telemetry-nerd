"""Is now unusual for this time of day / week? Compare a window with the same phase of previous
cycles (bead lkn.2). Pure numpy, no I/O.

Uncertainty comes from the spread ACROSS previous cycles at the same phase (leave-one-cycle-out
residuals), never from one reference cycle. Gaps are never interpolated. Design:
docs/superpowers/specs/2026-10-02-seasonal-compare-design.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations, pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

from telemetry_nerd.analysis.autocorr import n_eff, tau_int
from telemetry_nerd.analysis.sources import COMMON, MEASUREMENT, SPECIAL, UNDETERMINED, item
from telemetry_nerd.analysis.stats import MAD_SCALE, binom_sf, t_quantile

SCHEMES = ("previous", "1d", "1w")  # preference order: a later scheme must win by CHOICE_GAIN
DEFAULT_K = {"previous": 4, "1d": 7, "1w": 4}
PERIOD_DAYS = {"1d": 1, "1w": 7}
MIN_CYCLES = 3
MIN_COVERAGE = 0.5  # a cycle with data at fewer grid points is "missing"
CHOICE_GAIN = 0.95  # a more specific scheme must cut the LOO error by >= 5%
BAND = (0.05, 0.95)  # drawn: central 90% prediction band
ALPHA = 0.01  # per detector, per window
BLOCK_MIN = 100  # residuals per phase block
MAX_BLOCKS = 24
#: effective df per pooled residual for the extremes threshold; calibrated by seeded simulation
#: (k = 3..6, white and AR(0.7) noise): false-alarm rate <= nominal 1%
DF_SHARE = 0.2
#: forward search (lkn.8): two-sided level of each step's nearest-outsider test, by k; seeded
#: simulation (1e6 sets of k iid normal levels): P(any normal cycle excluded) = ALPHA
FORWARD_P = {4: 0.00248, 5: 0.00182, 6: 0.00095, 7: 0.000937, 8: 0.000865}
#: variance of the median of n iid N(0, 1) samples (simulated, 2e6 reps); pi / 2n beyond
_MEDIAN_VAR = {1: 1.0, 2: 0.5, 3: 0.4487, 4: 0.2982, 5: 0.2868, 6: 0.2147, 7: 0.2104, 8: 0.1682}


def _median_var(n: int) -> float:
    return _MEDIAN_VAR.get(n, math.pi / (2 * n))


def loo_factor(k: int) -> float:
    """LOO residuals predict from k-1 cycles, now is compared with the median of k."""
    return math.sqrt((1 + _median_var(k)) / (1 + _median_var(k - 1))) if k > 1 else 1.0


# alignment ------------------------------------------------------------------------------
def cycle_shifts(
    start_ms: int, scheme: str, k: int, tz: str, span_ms: int, step_ms: int
) -> list[int]:
    """UTC shift of each previous cycle j = 1..k (positive: cycle start = start - shift).

    `1d` / `1w` move by local calendar days in `tz`, so across a DST change the shift is 23 h or
    25 h and "the same local hour" stays the same local hour. `previous` moves by the span."""
    if scheme not in SCHEMES:
        raise ValueError(f"unknown cycle {scheme!r}: use {', '.join(SCHEMES)}")
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {tz!r} (hint: an IANA name like Europe/Berlin)") from e
    if scheme == "previous":
        return [j * span_ms for j in range(1, k + 1)]
    days = PERIOD_DAYS[scheme]
    if span_ms > days * 86_400_000:
        raise ValueError(
            f"the time range ({span_ms // 3_600_000}h) is longer than the {scheme} cycle: previous "
            "cycles would overlap it (hint: a shorter time range or a longer cycle)"
        )
    local = datetime.fromtimestamp(start_ms / 1000, UTC).astimezone(zone)
    wall = local.replace(tzinfo=None)
    out = []
    for j in range(1, k + 1):
        then = (wall - timedelta(days=days * j)).replace(tzinfo=zone)
        shift = start_ms - int(then.astimezone(UTC).timestamp() * 1000)
        if shift % step_ms:
            raise ValueError(
                f"cycle -{j}x{scheme} in {tz} is {shift / 3_600_000:g}h back, not a whole number "
                "of steps (hint: re-query at a step dividing 1h)"
            )
        out.append(shift)
    return out


def _wall_to_utc(wall: datetime, zone: ZoneInfo) -> int | None:
    """UTC ms of a local wall-clock time; None when it did not exist (spring forward). A time
    that occurred twice (autumn) takes its first occurrence."""
    aware = wall.replace(tzinfo=zone, fold=0)
    u = int(aware.timestamp() * 1000)
    back = datetime.fromtimestamp(u / 1000, UTC).astimezone(zone).replace(tzinfo=None)
    return u if back == wall else None


def point_shifts(start_ms: int, n: int, step_ms: int, scheme: str, k: int, tz: str) -> np.ndarray:
    """UTC shift of every point of every previous cycle, shape (k, n), NaN where the same local
    wall-clock time did not exist that day (lkn.8).

    Each point is matched by local wall clock: point i at UTC t_i is compared with the instant
    whose local time is local(t_i) - j days. Across a DST change inside the window (or inside a
    previous cycle) the shift changes by the offset difference, so every point keeps its local
    hour, not only the first. UTC and `previous` shifts are constant."""
    span = n * step_ms
    first = cycle_shifts(start_ms, scheme, k, tz, span, step_ms)  # validates, refuses
    out = np.tile(np.asarray(first, float)[:, None], (1, n))
    if scheme == "previous" or tz == "UTC":
        return out
    zone = ZoneInfo(tz)
    days = PERIOD_DAYS[scheme]
    walls = [
        datetime.fromtimestamp((start_ms + i * step_ms) / 1000, UTC).astimezone(zone).replace(tzinfo=None)
        for i in range(n)
    ]  # fmt: skip
    for j in range(1, k + 1):
        back = timedelta(days=days * j)
        for i, wall in enumerate(walls):
            u = _wall_to_utc(wall - back, zone)
            if u is None:
                out[j - 1, i] = np.nan
                continue
            sh = start_ms + i * step_ms - u
            if sh % step_ms:
                raise ValueError(
                    f"cycle -{j}x{scheme} in {tz} is {sh / 3_600_000:g}h back at one point, not a "
                    "whole number of steps (hint: re-query at a step dividing 1h)"
                )
            out[j - 1, i] = sh
    return out


def crosses_dst(shifts: np.ndarray) -> bool:
    """Some cycle's points do not share one shift (a DST change inside now or that cycle)."""
    return bool(np.any(np.isnan(shifts))) or any(
        np.unique(row[~np.isnan(row)]).size > 1 for row in shifts
    )


# comparison -----------------------------------------------------------------------------
@dataclass
class Cycle:
    scheme: str
    j: int  # 1 = most recent previous cycle
    shift_ms: int
    start_ms: int
    values: np.ndarray  # on now's grid, NaN where missing
    shifts_ms: np.ndarray | None = None  # per point (wall-clock alignment); None = shift_ms


@dataclass(frozen=True)
class Level:
    d0: float  # mean log-ratio (or difference) of now vs the centre
    ratio: float  # exp(d0) on a log scale, else the difference
    normal: tuple[float, float]  # 90% prediction interval for a normal cycle, same units
    p_interval: tuple[float, float]  # 99%
    cycle_means: list[float]  # each kept cycle's level vs the centre, same units as d0
    flagged: bool


@dataclass(frozen=True)
class Extremes:
    threshold: float  # |z| above this is flagged
    sidak: float  # threshold before the heavy-tail guard
    points: list[tuple[int, float]]  # (phase index, z)
    previous_max: list[float]  # largest |z| each previous cycle reached (LOO)
    heavy_tails: bool
    flagged: bool


@dataclass(frozen=True)
class OutsideBand:
    share: float  # of now's compared points outside the 90% band
    previous: list[float]  # each previous cycle's share (LOO)
    p: float  # binomial on n_eff against 10%
    flagged: bool


@dataclass
class Comparison:
    scheme: str
    verdict: str  # insufficient_history | unusual | usual
    reasons: list[str]
    kept: list[Cycle] = field(default_factory=list)
    excluded: list[tuple[Cycle, str]] = field(default_factory=list)  # missing | user | atypical
    scale: str = "log"
    direction: str | None = None  # higher | lower | mixed
    centre: np.ndarray | None = None  # natural units
    lo: np.ndarray | None = None
    hi: np.ndarray | None = None
    d: np.ndarray | None = None  # now vs centre: log-ratio or difference
    d_lo: np.ndarray | None = None
    d_hi: np.ndarray | None = None
    level: Level | None = None
    extremes: Extremes | None = None
    outside: OutsideBand | None = None
    n: int = 0
    n_eff: float = math.nan
    tau: float = math.nan
    score: float = math.nan  # LOO mean absolute error before atypical exclusion (choice)
    blocks: int = 1
    caveats: list[str] = field(default_factory=list)
    #: labelled findings (spec §5.4): {source, finding}
    variation: list[dict] = field(default_factory=list)


#: why a previous cycle was left out -> the source of that cycle's deviation (spec §5.4)
EXCLUDED_SOURCE = {
    "missing": MEASUREMENT,  # too little data in the cycle: coverage, not the process
    "atypical": SPECIAL,  # its level is beyond the others' prediction: a special cause back then
    "user": UNDETERMINED,  # the user left it out; the data does not say why
}


def _excluded_variation(excluded: list[tuple[Cycle, str]]) -> list[dict]:
    what = {
        "missing": "data at fewer than half of the phases",
        "atypical": "its level is beyond the other cycles' prediction (a holiday or incident "
        "back then), left out of the envelope",
        "user": "excluded by the user",
    }
    return [
        item(EXCLUDED_SOURCE[why], f"previous {c.scheme} cycle {c.j}: {what[why]}", cycle=c.j)
        for c, why in excluded
    ]


def scale_of(now: np.ndarray, cycles: list[Cycle]) -> str:
    vals = np.concatenate([now, *[c.values for c in cycles]])
    vals = vals[np.isfinite(vals)]
    return "log" if vals.size and bool(np.all(vals > 0)) else "linear"


def _g(x: np.ndarray, scale: str) -> np.ndarray:
    x = np.asarray(x, float)
    if scale == "log":
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(x > 0, np.log(np.where(x > 0, x, 1.0)), np.nan)
    return x


def _ginv(x: np.ndarray, scale: str) -> np.ndarray:
    return np.exp(x) if scale == "log" else x


def _nanmedian_min(x: np.ndarray, need: int) -> np.ndarray:
    """Column median where at least `need` rows are observed, else NaN."""
    cnt = np.sum(~np.isnan(x), axis=0)
    out = np.full(x.shape[1], np.nan)
    ok = cnt >= need
    if ok.any():
        out[ok] = np.nanmedian(x[:, ok], axis=0)
    return out


def _loo(X: np.ndarray) -> np.ndarray:
    k = X.shape[0]
    R = np.full_like(X, np.nan)
    for j in range(k):
        others = np.delete(X, j, axis=0)
        R[j] = X[j] - _nanmedian_min(others, min(MIN_CYCLES - 1, k - 1))
    return R


def _tau(R: np.ndarray) -> float:
    taus = []
    for r in R:
        ok = ~np.isnan(r)
        if ok.sum() >= 8:
            taus.append(tau_int(np.flatnonzero(ok), r[ok]))
    return float(np.mean(taus)) if taus else 1.0


def _blocks(R: np.ndarray) -> list[slice]:
    n = R.shape[1]
    nb = min(MAX_BLOCKS, int(np.sum(~np.isnan(R))) // BLOCK_MIN, n)
    if nb < 2:
        return [slice(0, n)]
    edges = np.linspace(0, n, nb + 1).round().astype(int)
    return [slice(int(a), int(b)) for a, b in pairwise(edges)]


def _block_stats(R: np.ndarray, blocks: list[slice], f: float):
    """Per phase: Weibull 5%/95% quantiles, median and robust sigma of the block's residuals,
    rescaled by the LOO factor f."""
    n_pts = R.shape[1]
    q_lo, q_hi, med, sig = (np.full(n_pts, np.nan) for _ in range(4))
    for b in blocks:
        pool = R[:, b][~np.isnan(R[:, b])]
        if pool.size < 2:
            continue
        lo_, hi_ = np.quantile(pool, BAND, method="weibull")
        m = float(np.median(pool))
        q_lo[b], q_hi[b] = f * lo_, f * hi_
        med[b], sig[b] = m, f * MAD_SCALE * float(np.median(np.abs(pool - m)))
    return q_lo, q_hi, med, sig


def atypical_levels(lev: np.ndarray, floor: float = 0.0) -> list[int]:
    """Indices of atypical cycles among levels `lev` (lkn.8): forward search from a clean subset.

    Start from the k-2 cycles whose levels vary least (k-1 for k <= 5: two atypical among five
    are out of reach of a 1% test anyway, and the extra step would cost a single outlier power),
    so up to two atypical cycles (Saturday and Sunday among daily references) cannot inflate the
    spread they are judged by. Then repeatedly test the outsider nearest the subset's mean against a Student-t
    prediction interval of the subset (sigma never below `floor`) and add it while it fits; at the
    first misfit every remaining cycle is atypical. The per-step level is calibrated by seeded
    simulation so that a set of normal cycles loses one with probability ALPHA (FORWARD_P)."""
    k = len(lev)
    if k <= MIN_CYCLES:
        return []
    h = k - 1 if k <= 5 else k - 2
    start = min(combinations(range(k), h), key=lambda s: (float(np.var(lev[list(s)], ddof=1)), s))
    alive = list(start)
    p = FORWARD_P.get(k, FORWARD_P[max(FORWARD_P)])
    while len(alive) < k:
        m = len(alive)
        sub = lev[alive]
        mu = float(np.mean(sub))
        half = max(float(np.std(sub, ddof=1)), floor) * math.sqrt(1 + 1 / m)
        rest = [j for j in range(k) if j not in alive]
        u = [(abs(float(lev[j]) - mu) / half if half > 0 else 0.0, j) for j in rest]
        best, j = min(u)
        if best > t_quantile(1 - p / 2, m - 1):
            break
        alive.append(j)
    return sorted(set(range(k)) - set(alive))


def _atypical(X: np.ndarray, R: np.ndarray) -> list[int]:
    """Cycles whose level (mean deviation from the median of all cycles) is atypical; sigma never
    below the SE of a cycle mean from point noise (identical cycles must not make every wobble
    atypical)."""
    c = _nanmedian_min(X, MIN_CYCLES)
    lev = np.array([float(np.nanmean(x - c)) for x in X])
    n_obs = max(1, int(np.sum(~np.isnan(X[0] - c))))
    s_pt = MAD_SCALE * float(np.nanmedian(np.abs(R - np.nanmedian(R))))
    return atypical_levels(lev, s_pt * math.sqrt(_tau(R) / n_obs))


def _block_n(R: np.ndarray, blocks: list[slice]) -> list[int]:
    return [int(np.sum(~np.isnan(R[:, b]))) for b in blocks]


def _level(
    X: np.ndarray, c: np.ndarray, d: np.ndarray, obs: np.ndarray, scale: str
) -> tuple[Level, float]:
    """Now's mean deviation from the centre against a Student-t prediction interval from the
    cycles' levels (k-1 df); also the cycles' mean level (dbar)."""
    k = X.shape[0]
    d0 = float(np.nanmean(d))
    # cycle levels relative to the common centre (in-sample: their spread is the spread of the
    # cycle levels); now - median of k cycles has variance sigma^2 (1 + v_k)
    Dl = np.array([float(np.nanmean(x[obs] - c[obs])) for x in X])
    dbar, sd = float(np.mean(Dl)), float(np.std(Dl, ddof=1))
    w = math.sqrt(1 + _median_var(k))
    h90 = t_quantile(0.95, k - 1) * sd * w
    h99 = t_quantile(1 - ALPHA / 2, k - 1) * sd * w
    conv = (lambda v: float(math.exp(v))) if scale == "log" else float
    level = Level(
        d0, conv(d0), (conv(dbar - h90), conv(dbar + h90)), (conv(dbar - h99), conv(dbar + h99)),
        [conv(v) for v in Dl], not dbar - h99 <= d0 <= dbar + h99,
    )  # fmt: skip
    return level, dbar


def _extremes(
    dc: np.ndarray,
    Rc: np.ndarray,
    b_med: np.ndarray,
    b_sig: np.ndarray,
    block_n: list[int],
    n: int,
    tau: float,
) -> Extremes:
    """Points of now beyond a Sidak threshold on the phase blocks' robust z, raised to the
    largest |z| a previous cycle reached when those exceed it (heavy tails)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (dc - b_med) / b_sig
        zr = (Rc - b_med) / b_sig
    # Sidak over every point (exact for independent points, conservative under correlation);
    # sigma is a MAD estimate from the block pool: Student t on its effective df = pool / tau x
    # DF_SHARE (MAD is 37% efficient, and LOO residuals sharing a phase are dependent)
    df = max(3.0, DF_SHARE * float(np.min(block_n)) / max(1.0, tau))
    sidak = t_quantile(1 - ALPHA / (2 * max(1, n)), df)
    prev_max = [float(np.nanmax(np.abs(r))) if np.any(np.isfinite(r)) else 0.0 for r in zr]
    heavy = any(m > sidak for m in prev_max)
    thr = max([sidak, *prev_max]) if heavy else sidak
    pts = [(int(i), float(z[i])) for i in np.flatnonzero(np.isfinite(z) & (np.abs(z) > thr))]
    return Extremes(thr, sidak, pts, prev_max, heavy, bool(pts))


def _outside_band(
    outside_now: np.ndarray,
    Rc: np.ndarray,
    w_lo: np.ndarray,
    w_hi: np.ndarray,
    n: int,
    effective_n: float,
) -> OutsideBand:
    """Share of now's points outside the 90% band: binomial on n_eff against 10%, and beyond
    every previous cycle's share."""
    share = float(outside_now.sum() / n)
    prev_share = []
    for r in Rc:
        ok = ~np.isnan(r) & ~np.isnan(w_lo)
        prev_share.append(float(np.mean((r[ok] < w_lo[ok]) | (r[ok] > w_hi[ok]))) if ok.any() else 0.0)  # fmt: skip
    ne = max(1, round(effective_n))
    p = binom_sf(round(share * ne), ne, 1 - (BAND[1] - BAND[0]))
    return OutsideBand(share, prev_share, p, p < ALPHA and share > max(prev_share))


def compare(
    now: np.ndarray,
    cycles: list[Cycle],
    step_ms: int,
    exclude: set[int] | frozenset[int] = frozenset(),
    scale: str | None = None,
) -> Comparison:
    """Now vs the kept cycles of one scheme. `exclude`: cycle indices j the user excluded."""
    now = np.asarray(now, float)
    scheme = cycles[0].scheme if cycles else "?"
    scale = scale or scale_of(now, cycles)
    excluded: list[tuple[Cycle, str]] = []
    kept: list[Cycle] = []
    for c in cycles:
        if c.j in exclude:
            excluded.append((c, "user"))
        elif np.mean(np.isfinite(c.values)) < MIN_COVERAGE:
            excluded.append((c, "missing"))
        else:
            kept.append(c)
    out = Comparison(scheme, "insufficient_history", [], kept, excluded, scale)
    if excluded:
        out.caveats.append("cycles_excluded")
    if len(kept) < MIN_CYCLES:
        out.reasons.append(
            f"{len(kept)} usable previous {scheme} cycle{'s' if len(kept) != 1 else ''} "
            f"(< {MIN_CYCLES}); {len(cycles)} looked at"
            + (f", {len(excluded)} excluded" if excluded else "")
        )
        out.variation = _excluded_variation(excluded)
        return out

    X = np.vstack([_g(c.values, scale) for c in kept])
    R = _loo(X)
    # the scheme's honest error includes its atypical cycles (a weekend among daily references)
    out.score = float(np.nanmean(np.abs(R)))
    # atypical cycles (holidays, incidents in the reference): each cycle's level against a
    # Student-t prediction interval from the others, Bonferroni over the cycles
    drop = _atypical(X, R)
    if drop:
        for idx in sorted(drop):
            excluded.append((kept[idx], "atypical"))
        kept = [c for i, c in enumerate(kept) if i not in drop]
        X = np.delete(X, drop, axis=0)
        R = _loo(X)
        if "cycles_excluded" not in out.caveats:
            out.caveats.append("cycles_excluded")
    out.kept, out.excluded = kept, excluded
    out.variation = _excluded_variation(excluded)
    k = len(kept)
    f = loo_factor(k)
    c = _nanmedian_min(X, MIN_CYCLES)
    y = _g(now, scale)
    d = y - c
    blocks = _blocks(R)
    out.blocks = len(blocks)
    q_lo, q_hi, _, _ = _block_stats(R, blocks, f)
    out.centre = _ginv(c, scale)
    out.lo, out.hi = _ginv(c + q_lo, scale), _ginv(c + q_hi, scale)
    out.d, out.d_lo, out.d_hi = d, q_lo, q_hi
    pool_n = int(np.sum(~np.isnan(R)))
    if pool_n < 200:
        out.caveats.append("small_residual_pool")

    obs = ~np.isnan(d)
    out.n = int(obs.sum())
    out.tau = _tau(R)
    out.n_eff = n_eff(out.n, out.tau)
    if out.n == 0:
        out.reasons.append("no phase where now and >= 3 cycles have data")
        return out

    # 1. level over the window ------------------------------------------------------------
    level, dbar = _level(X, c, d, obs, scale)

    # 2./3. within-window shape: each cycle relative to its own (median) level, so the
    # cycle-level component (judged by 1. with k-1 df) does not count once per point; the
    # median keeps a deviation over part of the window from moving the level it is judged by
    Rc = R - np.nanmedian(R, axis=1)[:, None]
    dc = d - float(np.nanmedian(d))
    w_lo, w_hi, b_med, b_sig = _block_stats(Rc, blocks, f)
    extremes = _extremes(dc, Rc, b_med, b_sig, _block_n(Rc, blocks), out.n, out.tau)
    if extremes.heavy_tails:
        out.caveats.append("heavy_tails")
    outside_now = obs & ((dc < w_lo) | (dc > w_hi))
    outside = _outside_band(outside_now, Rc, w_lo, w_hi, out.n, out.n_eff)

    out.level, out.extremes, out.outside = level, extremes, outside
    out.variation.insert(0, item(
        COMMON, f"normal band: the spread across {k} previous cycles at the same phase "
        "(cycle-to-cycle variation): the common-cause envelope",
    ))  # fmt: skip
    signs: set[str] = set()
    fmt = (lambda v: f"x{v:.3g}") if scale == "log" else (lambda v: f"{v:+.3g}")
    if level.flagged:
        signs.add("higher" if level.d0 > dbar else "lower")
        out.reasons.append(
            f"window level {fmt(level.ratio)} vs the reference; normal cycles "
            f"{fmt(level.normal[0])}..{fmt(level.normal[1])} (90%, from {k} cycles)"
        )
        out.variation.append(item(SPECIAL, out.reasons[-1]))
    if extremes.flagged:
        pts = extremes.points
        signs |= {"higher" if zz > 0 else "lower" for _, zz in pts}
        top = max(pts, key=lambda q: abs(q[1]))
        out.reasons.append(
            f"{len(pts)} point(s) beyond |z| {extremes.threshold:.2f} (max {top[1]:+.1f} at "
            f"phase {top[0]}); previous cycles reached at most {max(extremes.previous_max):.1f}"
        )
        out.variation.append(item(SPECIAL, out.reasons[-1]))
    if outside.flagged:
        dirs = dc[outside_now] - (w_lo + w_hi)[outside_now] / 2
        signs |= {"higher" if v > 0 else "lower" for v in dirs}
        out.reasons.append(
            f"{outside.share:.0%} of points outside the 90% shape band (previous cycles "
            f"{min(outside.previous):.0%}..{max(outside.previous):.0%}, p={outside.p:.1g} on "
            f"n_eff {out.n_eff:.0f})"
        )
        out.variation.append(item(SPECIAL, out.reasons[-1]))
    if signs:
        out.verdict = "unusual"
        if level.flagged:  # shape detectors are relative to the window's own level
            out.direction = "higher" if level.d0 > dbar else "lower"
        else:
            out.direction = next(iter(signs)) if len(signs) == 1 else "mixed"
    else:
        out.verdict = "usual"
        out.reasons.append(
            f"within normal: level {fmt(level.ratio)} (normal {fmt(level.normal[0])}.."
            f"{fmt(level.normal[1])}), {outside.share:.0%} outside the 90% shape band, no "
            "extreme points"
        )
        out.variation.append(item(COMMON, out.reasons[-1]))
    return out


def choose(
    now: np.ndarray,
    schemes: dict[str, list[Cycle]],
    step_ms: int,
    exclude: dict[str, set[int]] | None = None,
) -> tuple[str | None, dict[str, float]]:
    """Pick the reference by leave-one-cycle-out error, in SCHEMES order: a more specific
    scheme must beat the current pick by >= 5%. Returns (scheme or None, scores)."""
    scale = scale_of(now, [c for cs in schemes.values() for c in cs])
    scores: dict[str, float] = {}
    for s in SCHEMES:
        if s not in schemes or not schemes[s]:
            continue
        cmp_ = compare(now, schemes[s], step_ms, (exclude or {}).get(s, frozenset()), scale)
        if cmp_.verdict != "insufficient_history" and math.isfinite(cmp_.score):
            scores[s] = cmp_.score
    return pick_scheme(scores), scores


def pick_scheme(scores: dict[str, float]) -> str | None:
    """The reference among the scored schemes, in SCHEMES order: a more specific scheme must beat
    the current pick by >= 5% (CHOICE_GAIN)."""
    pick = None
    for s in SCHEMES:
        if s in scores and (pick is None or scores[s] < CHOICE_GAIN * scores[pick]):
            pick = s
    return pick
