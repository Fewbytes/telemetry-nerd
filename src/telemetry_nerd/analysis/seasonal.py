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
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

from telemetry_nerd.analysis.autocorr import tau_int
from telemetry_nerd.analysis.stability import MAD_SCALE, t_ppf

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
#: variance of the median of n iid N(0, 1) samples (simulated, 2e6 reps); pi / 2n beyond
#: effective df per pooled residual for the extremes threshold; calibrated by seeded simulation
#: (k = 3..6, white and AR(0.7) noise): false-alarm rate <= nominal 1%
DF_SHARE = 0.2
_MEDIAN_VAR = {1: 1.0, 2: 0.5, 3: 0.4487, 4: 0.2982, 5: 0.2868, 6: 0.2147, 7: 0.2104, 8: 0.1682}


def t_quantile(p: float, df: float) -> float:
    """Student t quantile: exact for df 1 and 2, Cornish-Fisher beyond (stability.t_ppf)."""
    if df <= 1:
        return math.tan(math.pi * (p - 0.5))
    if df <= 2:
        return (2 * p - 1) / math.sqrt(2 * p * (1 - p))
    return t_ppf(p, df)


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
            f"the window ({span_ms // 3_600_000}h) is longer than the {scheme} cycle: previous "
            "cycles would overlap it (hint: a shorter window or a longer cycle)"
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


def dst_within(start_ms: int, end_ms: int, tz: str) -> bool:
    zone = ZoneInfo(tz)
    a = datetime.fromtimestamp(start_ms / 1000, UTC).astimezone(zone).utcoffset()
    b = datetime.fromtimestamp(end_ms / 1000, UTC).astimezone(zone).utcoffset()
    return a != b


# comparison -----------------------------------------------------------------------------
@dataclass
class Cycle:
    scheme: str
    j: int  # 1 = most recent previous cycle
    shift_ms: int
    start_ms: int
    values: np.ndarray  # on now's grid, NaN where missing


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


def _atypical(X: np.ndarray, R: np.ndarray) -> list[int]:
    """Indices of cycles whose level (mean deviation from the median of all cycles) lies outside
    the (1 - ALPHA/k) Student-t prediction interval of the other cycles' levels; worst first,
    while more than MIN_CYCLES remain. sigma never below the SE of a cycle mean from point noise
    (identical cycles must not make every wobble atypical). False exclusion rate ~ALPHA."""
    k = X.shape[0]
    c = _nanmedian_min(X, MIN_CYCLES)
    lev = np.array([float(np.nanmean(x - c)) for x in X])
    n_obs = max(1, int(np.sum(~np.isnan(X[0] - c))))
    s_pt = MAD_SCALE * float(np.nanmedian(np.abs(R - np.nanmedian(R))))
    floor = s_pt * math.sqrt(_tau(R) / n_obs)
    alive = list(range(k))
    drop: list[int] = []
    while len(alive) > MIN_CYCLES:
        stats = []
        for j in alive:
            others = lev[[i for i in alive if i != j]]
            sd = max(float(np.std(others, ddof=1)), floor)
            half = t_quantile(1 - ALPHA / (2 * len(alive)), len(others) - 1)
            half *= sd * math.sqrt(1 + 1 / len(others))
            stats.append((abs(lev[j] - float(np.mean(others))) / half if half > 0 else 0.0, j))
        worst, j = max(stats)
        if worst <= 1:
            break
        drop.append(j)
        alive.remove(j)
    return drop


def _block_n(R: np.ndarray, blocks: list[slice]) -> list[int]:
    return [int(np.sum(~np.isnan(R[:, b]))) for b in blocks]


def _binom_sf(x: int, n: int, p: float) -> float:
    """P(Bin(n, p) >= x)."""
    if x <= 0:
        return 1.0
    if x > n:
        return 0.0
    return float(
        min(1.0, sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(x, n + 1)))
    )


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
    out.n_eff = out.n / max(1.0, out.tau)
    if out.n == 0:
        out.reasons.append("no phase where now and >= 3 cycles have data")
        return out

    # 1. level over the window ------------------------------------------------------------
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

    # 2./3. within-window shape: each cycle relative to its own (median) level, so the
    # cycle-level component (judged by 1. with k-1 df) does not count once per point; the
    # median keeps a deviation over part of the window from moving the level it is judged by
    Rc = R - np.nanmedian(R, axis=1)[:, None]
    dc = d - float(np.nanmedian(d))
    w_lo, w_hi, b_med, b_sig = _block_stats(Rc, blocks, f)

    # 2. extremes -------------------------------------------------------------------------
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (dc - b_med) / b_sig
        zr = (Rc - b_med) / b_sig
    # Sidak over every point (exact for independent points, conservative under correlation);
    # sigma is a MAD estimate from the block pool: Student t on its effective df = pool / tau x
    # DF_SHARE (MAD is 37% efficient, and LOO residuals sharing a phase are dependent)
    df = max(3.0, DF_SHARE * float(np.min(_block_n(Rc, blocks))) / max(1.0, out.tau))
    sidak = t_quantile(1 - ALPHA / (2 * max(1, out.n)), df)
    prev_max = [float(np.nanmax(np.abs(r))) if np.any(np.isfinite(r)) else 0.0 for r in zr]
    heavy = any(m > sidak for m in prev_max)
    thr = max([sidak, *prev_max]) if heavy else sidak
    pts = [(int(i), float(z[i])) for i in np.flatnonzero(np.isfinite(z) & (np.abs(z) > thr))]
    extremes = Extremes(thr, sidak, pts, prev_max, heavy, bool(pts))
    if heavy:
        out.caveats.append("heavy_tails")

    # 3. share outside the 90% band ----------------------------------------------------------
    outside_now = obs & ((dc < w_lo) | (dc > w_hi))
    share = float(outside_now.sum() / out.n)
    prev_share = []
    for r in Rc:
        ok = ~np.isnan(r) & ~np.isnan(w_lo)
        prev_share.append(float(np.mean((r[ok] < w_lo[ok]) | (r[ok] > w_hi[ok]))) if ok.any() else 0.0)  # fmt: skip
    ne = max(1, round(out.n_eff))
    p = _binom_sf(round(share * ne), ne, 1 - (BAND[1] - BAND[0]))
    outside = OutsideBand(share, prev_share, p, p < ALPHA and share > max(prev_share))

    out.level, out.extremes, out.outside = level, extremes, outside
    signs: set[str] = set()
    fmt = (lambda v: f"x{v:.3g}") if scale == "log" else (lambda v: f"{v:+.3g}")
    if level.flagged:
        signs.add("higher" if d0 > dbar else "lower")
        out.reasons.append(
            f"window level {fmt(level.ratio)} vs the reference; normal cycles "
            f"{fmt(level.normal[0])}..{fmt(level.normal[1])} (90%, from {k} cycles)"
        )
    if extremes.flagged:
        signs |= {"higher" if zz > 0 else "lower" for _, zz in pts}
        top = max(pts, key=lambda q: abs(q[1]))
        out.reasons.append(
            f"{len(pts)} point(s) beyond |z| {thr:.2f} (max {top[1]:+.1f} at phase {top[0]}); "
            f"previous cycles reached at most {max(prev_max):.1f}"
        )
    if outside.flagged:
        dirs = dc[outside_now] - (w_lo + w_hi)[outside_now] / 2
        signs |= {"higher" if v > 0 else "lower" for v in dirs}
        out.reasons.append(
            f"{share:.0%} of points outside the 90% shape band (previous cycles "
            f"{min(prev_share):.0%}..{max(prev_share):.0%}, p={p:.1g} on n_eff {out.n_eff:.0f})"
        )
    if signs:
        out.verdict = "unusual"
        if level.flagged:  # shape detectors are relative to the window's own level
            out.direction = "higher" if d0 > dbar else "lower"
        else:
            out.direction = next(iter(signs)) if len(signs) == 1 else "mixed"
    else:
        out.verdict = "usual"
        out.reasons.append(
            f"within normal: level {fmt(level.ratio)} (normal {fmt(level.normal[0])}.."
            f"{fmt(level.normal[1])}), {share:.0%} outside the 90% shape band, no extreme points"
        )
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
    pick = None
    for s in SCHEMES:
        if s not in schemes or not schemes[s]:
            continue
        cmp_ = compare(now, schemes[s], step_ms, (exclude or {}).get(s, frozenset()), scale)
        if cmp_.verdict == "insufficient_history" or not math.isfinite(cmp_.score):
            continue
        scores[s] = cmp_.score
        if pick is None or cmp_.score < CHOICE_GAIN * scores[pick]:
            pick = s
    return pick, scores
