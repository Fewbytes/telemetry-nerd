"""Judged points against the baseline when no control chart can (bead 7f15). Pure numpy.

A short series (24 points at 2 m) has too few baseline points for control limits (30), and an
episode inside it (latency up 100x for four steps, then back) fits neither the step nor the
trend model: its rise and fall stay in the residuals, so the series' autocorrelation time,
estimated on them, is the episode itself and the n_eff gate calls the clearest special cause
of the range "insufficient data". This test estimates the common-cause variation where the
episode is not: the baseline, and the residuals once the episode is fitted.

The excursion is the contiguous run of judged samples whose mean departs most from the
baseline centre (median), in units of its standard error; every run is scanned and the p value
is Bonferroni over all of them (n_j(n_j+1)/2; conservative, overlapping runs are positively
correlated). The run mean of L points has variance sigma^2 min(L, tau) / L (integrated
autocorrelation time tau, positive dependence), the centre (pi/2) sigma^2 tau / n_b. sigma is
taken at its one-sided 95% upper bound from the baseline's n_eff (1.4826 MAD, 37% efficient:
var(log sigma) = 1.3605 / n_eff), the conservative scenario SPC uses, so a short baseline does
not manufacture alarms. Two models, both reported (principle 16: results are model outputs):

- baseline: sigma and tau from the baseline deviations only, normal noise. Optimistic: tau
  from a short stretch is noisy and biased low, the judged window may be noisier than the
  baseline without any one episode, and telemetry noise is often skewed or heavy-tailed.
- cautious: tau = the larger of the baseline's and that of the whole series' residuals after
  fitting the excursion (the baseline centre outside the run, the run's own mean inside it);
  sigma = the larger of the baseline's and the robust sigma of every residual outside the run
  (the baseline and the rest of the judged window); noise as heavy-tailed as Student t with 4
  degrees of freedom. The label rests on it.

Special cause when the cautious p < ALPHA, undetermined when only the baseline model's is,
else common cause. Assumes the baseline is a stretch of the same process without the episode.
Calibration (scripts/calibrate_excursion.py, tests/unit/test_excursion.py): no-change series
of 24-60 points, half baseline, white / AR(1) 0.5, 0.8 / a rate window spanning 2 steps / t3 /
lognormal (sigma 0.5, 1) noise: cautious false alarms <= 0.12%, 0.88-1.0% for lognormal(1)
(the baseline model 0.7-4.8% for Gaussian noise, up to 20% under t3, 62% for lognormal(1)).
Power, a 4-step episode in 24 points: 20 sigma 60%, 30 sigma 90%, 100 sigma 100% (white).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from telemetry_nerd.analysis.autocorr import n_eff, tau_int
from telemetry_nerd.analysis.sources import SPECIAL, cautious_label
from telemetry_nerd.analysis.stability import ALPHA, MIN_SEGMENT
from telemetry_nerd.analysis.stats import MAD_VAR, robust_sigma, t_isf, t_sf

MEDIAN_VAR = math.pi / 2  # var(median) = (pi/2) sigma^2 / n for a normal
CONSERVATIVE_Z = 1.645  # sigma at its one-sided 95% upper bound (as SPC)
CAUTIOUS_DF = 4.0  # the cautious model's noise: tails as heavy as Student t4

METHOD = (
    "excursion of the judged points from the baseline: the contiguous run of judged samples "
    "whose mean departs most from the baseline median, scanned over every run (Bonferroni over "
    "n_j(n_j+1)/2 runs); run mean variance sigma^2 min(L, tau)/L, centre (pi/2) sigma^2 tau/n_b, "
    "sigma at its one-sided 95% upper bound (1.4826 MAD, from the baseline's n_eff). Two models: "
    "(a) baseline (sigma and autocorrelation time tau of the baseline deviations, normal noise); "
    "(b) cautious (tau = max(baseline, the whole series' residuals after fitting the run), "
    "sigma = max(baseline, robust sigma of every residual outside the run), noise tails as "
    "heavy as Student t4). The source label rests on (b): special cause when p < 0.01, "
    "undetermined when only (a) is; the run's delta from the baseline median with its 99% "
    "interval under (b)"
)


@dataclass(frozen=True)
class Model:
    """One model of the common-cause variation and its scan."""

    sigma: float  # as estimated (before the conservative scenario)
    sigma_used: float  # at its one-sided 95% upper bound
    tau: float
    df: float  # of the noise law (inf: normal)
    start: int  # run [start, end) in judged-sample positions
    end: int
    t: float  # the run's standardised departure (signed)
    p: float  # Bonferroni over the runs scanned


@dataclass(frozen=True)
class Excursion:
    start: int  # sample index of the run's first point
    end: int  # sample index after the run's last point
    start_ms: int
    end_ms: int  # timestamp of the run's last point
    #: judged points after the run, the last one back inside the cautious 3-sigma envelope of
    #: the baseline centre (it departed and came back); else it is still away at the end
    returned: bool
    n_baseline: int
    n_judged: int
    runs: int  # scanned (the Bonferroni factor)
    centre: float  # baseline median
    mean: float  # the run's mean
    delta: float  # mean - centre
    interval: tuple[float, float]  # of delta, 1 - ALPHA, cautious model
    baseline: Model
    cautious: Model

    @property
    def points(self) -> int:
        return self.end - self.start

    @property
    def p(self) -> float:
        return self.baseline.p

    @property
    def p_cautious(self) -> float:
        return self.cautious.p

    @property
    def status(self) -> str:
        return cautious_label(self.cautious.p < ALPHA, self.baseline.p < ALPHA)

    @property
    def significant(self) -> bool:
        """Significant under the cautious model (what a label may rest on)."""
        return self.status == SPECIAL

    @property
    def sigmas(self) -> float:
        """|delta| in the cautious model's sigmas (as estimated)."""
        return abs(self.delta) / self.cautious.sigma

    def describe(self) -> str:
        """The two model results, each with its model named (never as a fact)."""
        b, c = self.baseline, self.cautious
        return (
            f"against the baseline's own variation (normal noise, sigma {b.sigma:.3g}, tau "
            f"{b.tau:.2g} steps) p{_p(b.p)}; cautious (t4 noise, sigma {c.sigma:.3g}, tau "
            f"{c.tau:.2g} steps, from the residuals outside the run too) p{_p(c.p)}"
        )


def _p(p: float) -> str:
    return "<1e-300" if p < 1e-300 else f"={p:.2g}"


def _scan(x: np.ndarray, sigma: float, tau: float, n_b: int) -> tuple[int, int, float]:
    """The run [a, b) of `x` (deviations from the centre) with the largest |t|."""
    cs = np.r_[0.0, np.cumsum(x)]
    v_c = MEDIAN_VAR * tau / n_b
    best = (0, 1, 0.0)
    for L in range(1, x.size + 1):
        t = (cs[L:] - cs[:-L]) / L / (sigma * math.sqrt(min(L, tau) / L + v_c))
        i = int(np.argmax(np.abs(t)))
        if abs(t[i]) > abs(best[2]):
            best = (i, i + L, float(t[i]))
    return best


def _tail(t: float, df: float) -> float:
    return 0.5 * math.erfc(t / math.sqrt(2)) if math.isinf(df) else t_sf(t, df)


def _model(x, sigma, tau, n_b, runs, df) -> Model:
    s = sigma * math.exp(CONSERVATIVE_Z * math.sqrt(MAD_VAR / n_eff(n_b, tau)))
    a, b, t = _scan(x, s, tau, n_b)
    p = min(1.0, runs * 2 * _tail(abs(t), df)) if t else 1.0
    return Model(sigma, s, tau, df, a, b, t, p)


def excursion(
    ts_ms: np.ndarray,
    pos: np.ndarray,
    y: np.ndarray,
    baseline: np.ndarray,
    reference: tuple[np.ndarray, np.ndarray] | None = None,
) -> Excursion | None:
    """`pos`: each sample's step index; `baseline`: bool per sample. `reference` (pos, y): a
    separately fetched baseline (its own step indices); then every sample is judged. None
    unless the baseline holds >= MIN_SEGMENT points with a nonzero robust sigma and some point
    is judged."""
    y = np.asarray(y, float)
    if reference is not None:
        bpos, by = np.asarray(reference[0], np.int64), np.asarray(reference[1], float)
        judged = np.ones(y.size, bool)
    else:
        bpos, by = pos[baseline], y[baseline]
        judged = ~baseline
    jidx = np.flatnonzero(judged)
    nb, nj = int(by.size), int(jidx.size)
    if nb < MIN_SEGMENT or nj == 0:
        return None
    centre = float(np.median(by))
    sigma_b = robust_sigma(by)
    if sigma_b == 0:
        return None
    tau_b = tau_int(bpos, by - centre)
    runs = nj * (nj + 1) // 2
    x = y[jidx] - centre
    base = _model(x, sigma_b, tau_b, nb, runs, math.inf)
    # fit the excursion the baseline model found: the centre outside it, its own mean inside
    inside = np.zeros(y.size, bool)
    inside[jidx[base.start : base.end]] = True
    resid = y - centre
    resid[inside] -= float(resid[inside].mean())
    outside = resid[~inside] if reference is None else np.r_[by - centre, resid[~inside]]
    sigma_out = robust_sigma(outside) if outside.size >= MIN_SEGMENT else 0.0
    caut = _model(x, max(sigma_b, sigma_out), max(tau_b, tau_int(pos, resid)), nb, runs, CAUTIOUS_DF)  # fmt: skip
    run = jidx[caut.start : caut.end]
    mean = float(y[run].mean())
    delta = mean - centre
    L = run.size
    se = caut.sigma_used * math.sqrt(min(L, caut.tau) / L + MEDIAN_VAR * caut.tau / nb)
    half = t_isf(ALPHA / 2, CAUTIOUS_DF) * se
    back = caut.end < nj and abs(float(y[jidx[-1]]) - centre) <= 3 * caut.sigma_used
    return Excursion(
        int(run[0]), int(run[-1]) + 1, int(ts_ms[run[0]]), int(ts_ms[run[-1]]), back, nb, nj, runs, centre, mean, delta, (delta - half, delta + half), base,
        caut,
    )  # fmt: skip
