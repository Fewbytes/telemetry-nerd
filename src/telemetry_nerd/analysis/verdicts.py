"""Per-signal verdicts for a model binding (bead czt.4): did a golden signal move against its
reference cycles, how, and when. Pure numpy, no I/O.

Two detectors per signal, each with a stated alpha:

* **level**: now's window level against a Student-t prediction interval from the reference
  cycles' levels (random effects: their spread, never below their own sampling noise);
* **episode**: a two-sided tabular CUSUM over standardised per-step residuals relative to now's
  own robust level, run without restarts so an episode is one excursion; its threshold h is
  solved from the Brook-Evans ARL so that an in-control window of N blocks raises no episode
  with probability >= 1 - alpha (conservative scenario for the estimated centre and sigma).

Shares (RED errors, the share of requests above a latency threshold) are binomial counts per
step with effective n = n / (phi tau) (Pearson dispersion, autocorrelation); event rates are
quasi-Poisson; everything else is a per-step value. Onset = Page's estimator (the first block of
the excursion that fired), with an interval reaching back over a block and the rate window's
lookback and forward to the detection. Design:
docs/superpowers/specs/2026-10-02-binding-verdicts-design.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from telemetry_nerd.analysis.autocorr import ar1, tau_int
from telemetry_nerd.analysis.fraction import wilson
from telemetry_nerd.analysis.seasonal import MIN_CYCLES, atypical_levels
from telemetry_nerd.analysis.seasonal_dist import logit_share
from telemetry_nerd.analysis.spc import CONSERVATIVE_Z, MEDIAN_SE, cusum_arl
from telemetry_nerd.analysis.stability import ALPHA as CP_ALPHA
from telemetry_nerd.analysis.stability import changepoints
from telemetry_nerd.analysis.stats import MAD_SCALE, MAD_VAR, t_quantile, t_sf, z_of

CUSUM_K = 0.5
H_MAX = 30.0  # the Brook-Evans chain loses precision beyond (ARL ~ 1e13 at h = 30)
#: expected events per block for the normal approximation of a count residual
MIN_EXPECTED = 5.0
#: blocks needed for the episode detector
MIN_BLOCKS = 8
BLIP_BLOCKS = 2
#: blocks after its peak that close an episode still above 0 at the window's end
END_BLOCKS = 3
NEAR_BOUND = 0.9  # share of the natural bound that counts as "near" it
NEAR_RUN = 3  # steps in a row
#: Bai (1997): 97.5% quantile of the limiting changepoint-location distribution (in
#: (sigma / delta)^2 steps): a 95% interval for a single changepoint
BAI_95 = 11.03
REPORT = 0.95  # level of the reported intervals
MU_Z = 1.0  # now's centre off by this many standard errors in the ARL scenario


# level ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Level:
    scale: str  # logit | log | linear
    now: float
    centre: float  # mean of the kept reference levels
    sd: float  # prediction sd of a normal window's level
    df: int
    p: float
    flagged: bool
    previous: list[float]  # kept reference levels
    excluded: list[int]  # reference cycles left out as atypical (indices)

    @property
    def effect(self) -> float:
        return self.now - self.centre

    def half(self, level: float = REPORT) -> float:
        return t_quantile(1 - (1 - level) / 2, self.df) * self.sd

    def normal(self, level: float = 0.90) -> tuple[float, float]:
        h = self.half(level)
        return self.centre - h, self.centre + h

    def effect_interval(self, level: float = REPORT) -> tuple[float, float]:
        h = self.half(level)
        return self.effect - h, self.effect + h

    @property
    def direction(self) -> str:
        return "higher" if self.effect > 0 else "lower"


def level_test(
    l_now: float, v_now: float, l_refs: np.ndarray, v_refs: np.ndarray, alpha: float, scale: str
) -> Level | None:
    """Now's level against the reference levels; None with fewer than MIN_CYCLES kept."""
    ok = np.isfinite(l_refs) & np.isfinite(v_refs)
    idx = np.flatnonzero(ok)
    L, V = l_refs[ok], v_refs[ok]
    if L.size < MIN_CYCLES or not math.isfinite(l_now):
        return None
    drop = atypical_levels(L, math.sqrt(float(np.mean(V))))
    keep = np.array([i not in drop for i in range(L.size)])
    L, V = L[keep], V[keep]
    k = int(L.size)
    if k < MIN_CYCLES:
        return None
    m = float(np.mean(L))
    s2 = max(float(np.var(L, ddof=1)), float(np.mean(V)))
    var = s2 * (1 + 1 / k) + max(0.0, v_now - float(np.mean(V)))
    sd = math.sqrt(var)
    t = (l_now - m) / sd if sd > 0 else (0.0 if l_now == m else math.inf)
    p = min(1.0, 2 * t_sf(abs(t), k - 1)) if math.isfinite(t) else 0.0
    return Level(
        scale, l_now, m, sd, k - 1, p, p < alpha, [float(v) for v in L],
        [int(idx[i]) for i in drop],
    )  # fmt: skip


# episodes ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Episode:
    side: int  # +1 above now's level, -1 below
    start: int  # first block of the excursion (Page's change-time estimate)
    signal: int  # first block above h
    end: int | None  # block where the excursion peaked; None: still open at the window's end
    peak: float  # largest CUSUM value, in sigma units

    @property
    def blocks(self) -> int | None:
        return None if self.end is None else self.end - self.start + 1


def cusum_paths(z: np.ndarray, k: float = CUSUM_K) -> tuple[np.ndarray, np.ndarray]:
    """Upper and lower tabular CUSUM paths, never restarted. A missing block drains each side
    by k (its in-control drift), as in spc.cusum_signals: nothing is imputed."""
    up, dn = np.zeros(z.size), np.zeros(z.size)
    hi = lo = 0.0
    for i, v in enumerate(z):
        if math.isnan(v):
            hi, lo = max(0.0, hi - k), max(0.0, lo - k)
        else:
            hi, lo = max(0.0, hi + v - k), max(0.0, lo - v - k)
        up[i], dn[i] = hi, lo
    return up, dn


def _excursions(path: np.ndarray, h: float, side: int) -> list[Episode]:
    """Excursions of a CUSUM path above 0 that exceed h. An episode ends at the path's peak
    (after it the residuals average below k: back near now's level, the path only drains); it
    is still open when fewer than END_BLOCKS blocks follow the peak (no evidence it subsided)."""
    out: list[Episode] = []
    start: int | None = None
    peak, at, signal = 0.0, 0, None
    for i, v in enumerate(path):
        if v > 0 and start is None:
            start, peak, at, signal = i, 0.0, i, None
        if start is None:
            continue
        if v > peak:
            peak, at = float(v), i
        if v > h and signal is None:
            signal = i
        if v == 0:
            if signal is not None:
                out.append(Episode(side, start, signal, at, peak))
            start = None
    if start is not None and signal is not None:
        still = path.size - 1 - at < END_BLOCKS
        out.append(Episode(side, start, signal, None if still else at, peak))
    return out


def episodes(z: np.ndarray, h: float) -> list[Episode]:
    up, dn = cusum_paths(z)
    eps = _excursions(up, h, 1) + _excursions(dn, h, -1)
    return sorted(eps, key=lambda e: (e.signal, e.start))


def max_excursion(z: np.ndarray) -> float:
    up, dn = cusum_paths(z)
    return float(max(up.max(initial=0.0), dn.max(initial=0.0)))


def cusum_h(n_blocks: int, alpha: float, mu: float = 0.0, r: float = 1.0) -> float:
    """Smallest h whose two-sided in-control ARL (z ~ N(mu, r^2)) makes an episode within
    n_blocks at most alpha likely: n / ARL <= -ln(1 - alpha) (Poisson count of episodes)."""
    target = max(1, n_blocks) / -math.log1p(-alpha)
    mu, r = round(mu, 3), round(r, 3)
    lo, hi = 0.5, H_MAX
    if cusum_arl(CUSUM_K, hi, mu, r) < target:
        return hi
    for _ in range(30):
        mid = round(0.5 * (lo + hi), 4)
        if cusum_arl(CUSUM_K, mid, mu, r) >= target:
            hi = mid
        else:
            lo = mid
        if hi - lo < 0.01:
            break
    return hi


@dataclass
class EpisodeStage:
    episodes: list[Episode]
    h: float
    phi: float  # lag-1 autocorrelation of the reference residuals
    prewhitened: bool
    z: np.ndarray  # now's standardised residuals (before prewhitening), per block
    caveats: list[str] = field(default_factory=list)


def _concat(series: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Positions and values of several series on one axis, a gap between them (no cross pairs)."""
    pos, vals, off = [], [], 0
    for s in series:
        ok = np.flatnonzero(~np.isnan(s))
        pos.append(ok + off)
        vals.append(s[ok])
        off += s.size + 2
    if not pos:
        return np.array([], np.int64), np.array([])
    return np.concatenate(pos).astype(np.int64), np.concatenate(vals)


def _prewhiten(z: np.ndarray, phi: float) -> np.ndarray:
    prev = np.r_[np.nan, z[:-1]]
    return np.where(np.isnan(prev), np.nan, z - phi * prev)


def _scale(x: np.ndarray, robust: bool) -> float:
    x = x[~np.isnan(x)]
    if x.size < 3:
        return math.nan
    s = MAD_SCALE * float(np.median(np.abs(x - np.median(x)))) if robust else 0.0
    return s if s > 0 else float(np.std(x, ddof=1))


def episode_stage(
    z_now: np.ndarray, z_refs: list[np.ndarray], alpha: float, robust: bool
) -> EpisodeStage | None:
    """CUSUM episodes of now's standardised residuals; h for `alpha` over now's blocks, raised
    above every reference cycle's largest excursion (heavy tails)."""
    if int(np.sum(~np.isnan(z_now))) < MIN_BLOCKS:
        return None
    pos, vals = _concat(z_refs)
    if vals.size < MIN_BLOCKS:
        return None
    fit = ar1(pos, vals - float(np.mean(vals)))
    pre = fit.significant and fit.phi > 0
    phi = fit.phi if pre else 0.0
    w_refs = [_prewhiten(z, phi) for z in z_refs] if pre else z_refs
    w_now = _prewhiten(z_now, phi) if pre else z_now
    s = _scale(np.concatenate(w_refs), robust)
    if not math.isfinite(s) or s <= 0:
        return None
    w_refs = [w / s for w in w_refs]
    w_now = w_now / s
    caveats = []
    # maximalist: now's own robust spread when it is wider than the reference's (noise that
    # grows with the level, e.g. a share's variance at a higher share). From successive
    # differences: a level shift or an episode adds one or two outliers there, not a spread
    s_now = _scale(np.diff(w_now), True) / math.sqrt(2)
    if math.isfinite(s_now) and s_now > 1:
        w_now = w_now / s_now
        caveats.append("noisier_than_reference")
    n_now = int(np.sum(~np.isnan(w_now)))
    n_ref = int(sum(np.sum(~np.isnan(w)) for w in w_refs))
    # conservative scenario: now's centre (a median of n points) off by its one-sided 95% bound,
    # sigma at its one-sided 95% upper bound from the reference's points
    mu = MU_Z * MEDIAN_SE / math.sqrt(max(1, n_now))
    r = math.exp(CONSERVATIVE_Z * math.sqrt(MAD_VAR / max(1, n_ref)))
    h = cusum_h(n_now, alpha, mu, r)
    ref_peak = max((max_excursion(w) for w in w_refs), default=0.0)
    if ref_peak > h:
        h = ref_peak * 1.001
        caveats.append("heavy_tails")
    return EpisodeStage(episodes(w_now, h), h, float(phi), pre, z_now, caveats)


# per-step data -> blocks ------------------------------------------------------------------------
def block_sums(x: np.ndarray, b: int) -> np.ndarray:
    """Sums over consecutive blocks of b steps; a block with no observed step is NaN."""
    if b <= 1:
        return x.astype(float)
    nb = -(-x.size // b)
    pad = np.full(nb * b, np.nan)
    pad[: x.size] = x
    m = pad.reshape(nb, b)
    seen = np.sum(~np.isnan(m), axis=1)
    return np.where(seen > 0, np.nansum(m, axis=1), np.nan)


@dataclass(frozen=True)
class Onset:
    at_ms: int | None  # None: before the window (the whole window differs)
    lo_ms: int | None
    hi_ms: int
    basis: str  # cusum | changepoint | before_window


@dataclass
class Judgement:
    """One signal (or member) against its reference."""

    kind: str  # share | count | value
    status: str  # changed | no_change | insufficient
    level: Level | None = None
    stage: EpisodeStage | None = None
    direction: str | None = None  # higher | lower
    pattern: str | None = None  # level | shift | blip | burst | sustained
    onset: Onset | None = None
    block: int = 1  # steps per block
    n_eff_now: float | None = None
    now_value: float | None = None  # now's share / rate / mean on the natural scale
    now_interval: tuple[float, float] | None = None
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def episodes(self) -> list[Episode]:
        return self.stage.episodes if self.stage else []


def _shift(cp_y: np.ndarray, side: int | None) -> tuple[int, int, object] | None:
    """The first significant single changepoint (stability.changepoints) in direction `side`
    (any when None): (block, Bai 95% half-width in blocks, the shift)."""
    pos = np.flatnonzero(~np.isnan(cp_y))
    if pos.size == 0:
        return None
    for sh in changepoints(pos.astype(np.int64), pos.astype(np.int64), cp_y[pos]):
        if sh.delta == 0 or (side is not None and (sh.delta > 0) != (side > 0)):
            continue
        half = (sh.interval[1] - sh.interval[0]) / 2
        slr = half / (z_of(1 - CP_ALPHA / 2) * math.sqrt(1 / sh.n_before + 1 / sh.n_after))
        return int(pos[sh.index]), math.ceil(BAI_95 * (slr / abs(sh.delta)) ** 2) + 1, sh
    return None


def _finish(j: Judgement, ts: np.ndarray, step_ms: int, lookback_ms: int, cp_y: np.ndarray):
    """Direction, pattern and onset from the detectors.

    An episode relative to now's own (median) level that opens at the window's start, on the
    other side of the change, means the change covers most of the window: now's median is
    past it. Then the onset is the window's single changepoint (Bai's interval), not an
    excursion of noise around the new level."""
    lv, eps = j.level, j.episodes
    if not (lv and lv.flagged) and not eps:
        j.status = "no_change"
        return j
    j.status = "changed"
    b, n = j.block, ts.size
    block_ms = b * step_ms

    def start_of(i: int) -> int:
        return int(ts[min(i * b, n - 1)]) - step_ms

    def end_of(i: int) -> int:
        return int(ts[min((i + 1) * b, n) - 1])

    side = (1 if lv.effect > 0 else -1) if lv and lv.flagged else None
    first = eps[0] if eps else None
    past = first is not None and first.start <= 1 and (side is None or first.side != side)
    if past and first is not None and (cp := _shift(cp_y, -first.side)) is not None:
        blk, hw, _ = cp
        side = -first.side
        at = start_of(blk)
        hi = min(int(ts[-1]), at + hw * block_ms)
        j.onset = Onset(at, at - hw * block_ms - lookback_ms, hi, "changepoint")
        j.direction, j.pattern = ("higher" if side > 0 else "lower"), "shift"
        return j
    if side is None:
        side = first.side if first else 1
    j.direction = "higher" if side > 0 else "lower"
    mine = [e for e in eps if e.side == side]
    if mine:
        e = mine[0]
        at = start_of(e.start)
        j.onset = Onset(at, at - block_ms - lookback_ms, end_of(e.signal), "cusum")
        if e.end is None:
            j.pattern = "sustained"
        elif (e.blocks or 0) <= BLIP_BLOCKS:
            j.pattern = "blip"
        else:
            j.pattern = "burst"
        return j
    if (cp := _shift(cp_y, side)) is not None:
        blk, hw, _ = cp
        at = start_of(blk)
        hi = min(int(ts[-1]), at + hw * block_ms)
        j.onset = Onset(at, at - hw * block_ms - lookback_ms, hi, "changepoint")
        j.pattern = "shift"
        return j
    j.onset = Onset(None, None, int(ts[0]) - step_ms, "before_window")
    j.pattern = "level"
    return j


# shares and counts ------------------------------------------------------------------------------
def _link(a: float, n: float, family: str, infl: float) -> tuple[float, float]:
    """(level, variance) of a window's share (logit) or rate (log) on effective counts."""
    if family == "binomial":
        return logit_share(a / infl, n / infl)
    return math.log((a + 0.5) / n), infl / (a + 0.5)


def judge_counts(
    now: tuple[np.ndarray, np.ndarray],
    refs: list[tuple[np.ndarray, np.ndarray]],
    ts: np.ndarray,
    step_ms: int,
    *,
    family: str = "binomial",
    alpha_level: float,
    alpha_episode: float,
    lookback_ms: int = 0,
) -> Judgement:
    """Events a_t out of n_t per step: binomial (n = trials) or Poisson (n = exposure, s)."""
    kind = "share" if family == "binomial" else "count"
    j = Judgement(kind, "insufficient")
    a0, n0 = (np.asarray(x, float) for x in now)
    ok0 = ~np.isnan(a0) & ~np.isnan(n0) & (n0 > 0)
    A0, N0 = float(a0[ok0].sum()), float(n0[ok0].sum())
    rs = []
    for a, n in refs:
        a, n = np.asarray(a, float), np.asarray(n, float)
        ok = ~np.isnan(a) & ~np.isnan(n) & (n > 0)
        if ok.sum() >= max(2, ok0.sum() // 2):
            rs.append((np.where(ok, a, np.nan), np.where(ok, n, np.nan)))
    if N0 <= 0:
        j.reasons.append(
            "no requests in the window" if kind == "share" else "no data in the window"
        )
        return j
    if len(rs) < MIN_CYCLES:
        j.reasons.append(f"{len(rs)} usable reference windows (< {MIN_CYCLES})")
        return j
    A_r = sum(float(np.nansum(a)) for a, _ in rs)
    N_r = sum(float(np.nansum(n)) for _, n in rs)
    p_ref = A_r / N_r
    if A_r == 0 and A0 == 0:
        j.status = "no_change"
        j.now_value, j.now_interval = 0.0, None
        j.reasons.append("no events in the window or the reference windows (exact)")
        return j
    # blocks: >= MIN_EXPECTED events per block at the reference rate, for the normal residual
    med_n = float(np.nanmedian(np.concatenate([n for _, n in rs])))
    e_step = max(p_ref, 0.5 / N_r) * med_n
    b = max(1, math.ceil(MIN_EXPECTED / e_step)) if e_step > 0 else a0.size
    j.block = b
    a0[~ok0], n0[~ok0] = np.nan, np.nan
    blocks = [(block_sums(a, b), block_sums(n, b)) for a, n in rs]
    ab0, nb0 = block_sums(a0, b), block_sums(n0, b)

    def var_fn(n: np.ndarray, p: float) -> np.ndarray:
        return n * p * (1 - p) if family == "binomial" else n * p

    # dispersion and autocorrelation from the reference cycles
    phis, resid = [], []
    for ab, nb in blocks:
        ok = ~np.isnan(ab) & ~np.isnan(nb) & (nb > 0)
        p = float(ab[ok].sum() / nb[ok].sum())
        if p <= 0 or (family == "binomial" and p >= 1) or ok.sum() < 3:
            resid.append(np.full(ab.size, np.nan))
            continue
        r = np.where(ok, (ab - nb * p) / np.sqrt(var_fn(nb, p)), np.nan)
        phis.append(float(np.nansum(r**2)) / (ok.sum() - 1))
        resid.append(r)
    disp = max(1.0, float(np.median(phis))) if phis else 1.0
    pos, vals = _concat([r / math.sqrt(disp) for r in resid])
    tau = tau_int(pos, vals) if vals.size >= 8 else 1.0
    infl = disp * tau
    # Var(total) = phi tau x the binomial (Poisson) variance: phi and tau both from blocks
    lv_refs = [_link(float(np.nansum(a)), float(np.nansum(n)), family, infl) for a, n in rs]
    l0, v0 = _link(A0, N0, family, infl)
    j.level = level_test(
        l0, v0, np.array([x for x, _ in lv_refs]), np.array([v for _, v in lv_refs]),
        alpha_level, "logit" if family == "binomial" else "log",
    )  # fmt: skip
    j.n_eff_now = N0 / infl if family == "binomial" else A0 / infl
    j.now_value = A0 / N0
    if family == "binomial":
        j.now_interval = wilson(A0 / infl, N0 / infl)
    else:
        h = 1.96 * math.sqrt(max(A0, 0.5) * infl) / N0
        j.now_interval = (max(0.0, A0 / N0 - h), A0 / N0 + h)
    if disp > 1.5:
        j.caveats.append("overdispersed")
    if j.level is None:
        j.reasons.append("fewer than 3 typical reference windows")
    # episodes: Pearson residuals around now's own robust level (median block share)
    nblocks = int(np.sum(~np.isnan(nb0)))
    if nblocks >= MIN_BLOCKS:

        def z_of_cycle(ab: np.ndarray, nb: np.ndarray) -> np.ndarray:
            ok = ~np.isnan(ab) & ~np.isnan(nb) & (nb > 0)
            if ok.sum() < 3:
                return np.full(ab.size, np.nan)
            pc = float(np.median(ab[ok] / nb[ok]))
            if pc <= 0:
                pc = float(ab[ok].sum() / nb[ok].sum())
            pc = min(max(pc, 0.5 / float(nb[ok].sum())), 1 - 1e-9)
            return np.where(ok, (ab - nb * pc) / np.sqrt(disp * var_fn(nb, pc)), np.nan)

        z0 = z_of_cycle(ab0, nb0)
        j.stage = episode_stage(z0, [z_of_cycle(a, n) for a, n in blocks], alpha_episode, False)
        if j.stage:
            j.caveats += j.stage.caveats
    else:
        j.caveats.append("few_events_for_onsets")
    if j.level is None and j.stage is None:
        return j
    cp = j.stage.z if j.stage else np.full(1, np.nan)
    return _finish(j, ts, step_ms, lookback_ms, cp)


# values ---------------------------------------------------------------------------------------
def judge_values(
    now: np.ndarray,
    refs: list[np.ndarray],
    ts: np.ndarray,
    step_ms: int,
    *,
    alpha_level: float,
    alpha_episode: float,
    lookback_ms: int = 0,
    phase_aligned: bool = False,
) -> Judgement:
    """A per-step value (rate, utilization, queue length, concurrency) against the reference
    windows. Log scale when every value is > 0, else linear. With day/week references the
    within-window shape (median over the cycles, leave-one-out for each cycle) is removed first."""
    j = Judgement("value", "insufficient")
    y0 = np.asarray(now, float)
    rs = [np.asarray(r, float) for r in refs]
    n0 = int(np.sum(~np.isnan(y0)))
    rs = [r for r in rs if np.sum(~np.isnan(r)) >= max(2, n0 // 2)]
    if n0 < 3:
        j.reasons.append("fewer than 3 points in the window")
        return j
    if len(rs) < MIN_CYCLES:
        j.reasons.append(f"{len(rs)} usable reference windows (< {MIN_CYCLES})")
        return j
    allv = np.concatenate([y0[~np.isnan(y0)], *[r[~np.isnan(r)] for r in rs]])
    scale = "log" if np.all(allv > 0) else "linear"
    g = np.log if scale == "log" else (lambda x: x)
    G0, GR = g(y0), [g(r) for r in rs]
    k = len(GR)

    def shape(excl: int | None) -> np.ndarray:
        if not phase_aligned:
            return np.zeros(y0.size)
        mats = [x - np.nanmedian(x) for i, x in enumerate(GR) if i != excl]
        with np.errstate(all="ignore"):
            s = np.nanmedian(np.vstack(mats), axis=0)
        return np.nan_to_num(s)

    d0 = G0 - shape(None)
    w_refs = []
    for i, x in enumerate(GR):
        d = x - shape(i)
        w_refs.append(d - np.nanmedian(d))
    pooled = np.concatenate([w[~np.isnan(w)] for w in w_refs])
    sigma = _scale(pooled, True)
    pos, vals = _concat(w_refs)
    tau = tau_int(pos, vals) if vals.size >= 8 and np.std(vals) > 0 else 1.0
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = 0.0
    lv = [(float(np.nanmean(x)), sigma**2 * tau / max(1, int(np.sum(~np.isnan(x))))) for x in GR]
    l0 = float(np.nanmean(G0))
    v0 = sigma**2 * tau / n0
    j.level = level_test(
        l0, v0, np.array([a for a, _ in lv]), np.array([b for _, b in lv]), alpha_level, scale
    )
    j.now_value = float(np.nanmean(y0))
    j.n_eff_now = n0 / tau
    if sigma > 0:
        se = sigma * math.sqrt(tau / n0)
        lo, hi = l0 - 1.96 * se, l0 + 1.96 * se
        j.now_interval = (math.exp(lo), math.exp(hi)) if scale == "log" else (lo, hi)
        z0 = (d0 - np.nanmedian(d0)) / sigma
        j.stage = episode_stage(z0, [w / sigma for w in w_refs], alpha_episode, True)
        if j.stage:
            j.caveats += j.stage.caveats
    else:
        j.caveats.append("constant_reference")
    if j.level is None and j.stage is None:
        if k < MIN_CYCLES:
            j.reasons.append("fewer than 3 typical reference windows")
        return j
    cp = j.stage.z if j.stage else np.full(1, np.nan)
    return _finish(j, ts, step_ms, lookback_ms, cp)


# bounds ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NearBound:
    bound: float
    threshold: float
    steps: int  # steps at or above the threshold
    runs: list[tuple[int, int]]  # [first, last] step index of runs >= NEAR_RUN


def near_bound(y: np.ndarray, bound: float, frac: float = NEAR_BOUND) -> NearBound:
    """Runs of >= NEAR_RUN steps at or above frac x bound (exact counts; a rule, not a test)."""
    thr = frac * bound
    hot = np.nan_to_num(np.asarray(y, float), nan=-math.inf) >= thr
    runs, start = [], None
    for i, h in enumerate(np.r_[hot, False]):
        if h and start is None:
            start = i
        elif not h and start is not None:
            if i - start >= NEAR_RUN:
                runs.append((start, i - 1))
            start = None
    return NearBound(bound, thr, int(hot.sum()), runs)


# ordering -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Order:
    order: list[str]  # roles by onset estimate (before-window first)
    clusters: list[list[str]]  # roles whose onset intervals overlap, in order
    first: str | None  # a single first mover, when its interval clears every later one


def order_onsets(onsets: dict[str, Onset]) -> Order:
    """Order changed roles by onset; overlapping intervals are one cluster (not ordered)."""
    if not onsets:
        return Order([], [], None)

    def lo(o: Onset) -> float:
        return -math.inf if o.lo_ms is None else float(o.lo_ms)

    def at(o: Onset) -> float:
        return -math.inf if o.at_ms is None else float(o.at_ms)

    roles = sorted(onsets, key=lambda r: (at(onsets[r]), onsets[r].hi_ms, r))
    clusters: list[list[str]] = []
    reach = -math.inf
    for r in roles:
        o = onsets[r]
        if clusters and lo(o) <= reach:
            clusters[-1].append(r)
            reach = max(reach, float(o.hi_ms))
        else:
            clusters.append([r])
            reach = float(o.hi_ms)
    first = clusters[0][0] if len(clusters[0]) == 1 else None
    return Order(roles, clusters, first)


# a binding: the family ------------------------------------------------------------------------
@dataclass
class RoleInput:
    """One role's data on now's step grid: kind share | count -> (a, n) arrays per cycle; value
    -> y arrays. `members` (value roles judged per member): {member: (now, refs)}."""

    kind: str
    now: object = None
    refs: list = field(default_factory=list)
    members: dict[str, tuple[object, list]] | None = None
    lookback_ms: int = 0
    phase_aligned: bool = False
    bound: float | None = None  # natural bound (utilization 1 or 100; a saturation limit)


@dataclass
class RoleResult:
    judgement: Judgement
    alpha_level: float
    alpha_episode: float
    member: str | None = None  # per-member role: the member the judgement is
    members: list[tuple[str, Judgement]] = field(default_factory=list)
    near: dict[str, NearBound] = field(default_factory=dict)  # per member (or "" for the total)


@dataclass(frozen=True)
class Family:
    alpha: float
    roles: int
    per_role: float
    per_detector: float


def family(alpha: float, roles: int) -> Family:
    """Bonferroni over the judged roles; each role's budget split between its two detectors."""
    m = max(1, roles)
    return Family(alpha, m, alpha / m, alpha / (2 * m))


def _judge_one(inp: RoleInput, now, refs, ts, step_ms, a: float) -> Judgement:
    if inp.kind == "value":
        return judge_values(
            now, refs, ts, step_ms, alpha_level=a, alpha_episode=a,
            lookback_ms=inp.lookback_ms, phase_aligned=inp.phase_aligned,
        )  # fmt: skip
    return judge_counts(
        now, refs, ts, step_ms, family="binomial" if inp.kind == "share" else "poisson",
        alpha_level=a, alpha_episode=a, lookback_ms=inp.lookback_ms,
    )  # fmt: skip


def _rank(j: Judgement) -> tuple:
    """Representative member: changed first, earliest onset, then strongest level evidence."""
    on = j.onset.at_ms if j.onset and j.onset.at_ms is not None else -math.inf
    return (j.status != "changed", on if j.status == "changed" else 0, j.level.p if j.level else 1)


def judge_roles(
    inputs: dict[str, RoleInput], ts: np.ndarray, step_ms: int, alpha: float = 0.05
) -> tuple[dict[str, RoleResult], Family, Order]:
    """Every role against its reference at family-wise alpha, and the order of the onsets."""
    fam = family(alpha, len(inputs))
    out: dict[str, RoleResult] = {}
    for role, inp in inputs.items():
        if inp.members:
            a = fam.per_detector / len(inp.members)
            js = [
                (m, _judge_one(inp, now, refs, ts, step_ms, a))
                for m, (now, refs) in inp.members.items()
            ]
            best = min(js, key=lambda x: _rank(x[1]))
            res = RoleResult(best[1], a, a, best[0], js)
            nows = {m: now for m, (now, _) in inp.members.items()}
        else:
            a = fam.per_detector
            res = RoleResult(_judge_one(inp, inp.now, inp.refs, ts, step_ms, a), a, a)
            nows = {"": inp.now}
        if inp.bound is not None and inp.kind == "value":
            res.near = {m: near_bound(np.asarray(y, float), inp.bound) for m, y in nows.items()}
        out[role] = res
    onsets = {
        r: res.judgement.onset
        for r, res in out.items()
        if res.judgement.status == "changed" and res.judgement.onset is not None
    }
    return out, fam, order_onsets(onsets)


# latency threshold ------------------------------------------------------------------------------
#: the latency threshold: the reference's ~p95 bucket edge
SLOW_SHARE = 0.05
MIN_OVER = 20  # expected observations above the threshold per reference window


def share_threshold(
    refs: list, edges: np.ndarray, target: float = SLOW_SHARE
) -> tuple[float | None, bool]:
    """From the reference windows only (never from now): the common bucket edge whose pooled
    share above is nearest `target` (log distance) among edges with >= MIN_OVER observations
    above per window; else the nearest with fewer (second value False). `refs`: window
    histograms (analysis.seasonal_dist.Hist)."""
    refs = [h for h in refs if h.n > 0]
    if not refs or edges.size == 0:
        return None, False
    n = sum(h.n for h in refs)
    cands = []
    for e in edges:
        a = sum(h.over(e) for h in refs)
        if 0 < a < n:
            cands.append((abs(math.log((a / n) / target)), a / len(refs) >= MIN_OVER, float(e)))
    if not cands:
        return None, False
    enough = [c for c in cands if c[1]]
    pick = min(enough or cands)
    return pick[2], bool(enough)
