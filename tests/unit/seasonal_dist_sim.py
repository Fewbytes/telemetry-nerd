"""Seeded simulation of latency histograms per cycle (bead lkn.7), shared by the unit tests and
scripts/calibrate_seasonal.py.

Each cycle's window has `cols` steps; each step has Poisson(n / cols) requests from a lognormal
whose log-median jitters per cycle (sd `jitter`) and per step (sd 0.1: requests within a window
are not independent, so the binomial noise alone would be too optimistic). A planted tail moves
`tail` of now's requests x5 slower. Classic le buckets.
"""

from __future__ import annotations

import math

import numpy as np

from telemetry_nerd.analysis.seasonal_dist import (
    Hist,
    common_edges,
    compare_tail,
    default_threshold,
)

LE = np.array([0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0])
LO = np.concatenate([[-math.inf], LE])
HI = np.concatenate([LE, [math.inf]])
_ERF = np.vectorize(math.erf)


def _cdf(x: np.ndarray, mu: float, sigma: float) -> np.ndarray:
    with np.errstate(divide="ignore"):
        z = (np.log(np.where(x > 0, x, 1e-300)) - mu) / (sigma * math.sqrt(2))
    return 0.5 * (1 + _ERF(z))


def window_hist(rng, j: int, n: float, jitter: float, tail: float = 0.0, cols: int = 48,
                mu: float = math.log(0.08), sigma: float = 0.6) -> Hist:  # fmt: skip
    m = mu + rng.normal(0, jitter)
    counts = np.zeros(LO.size)
    for _ in range(cols):
        mc = m + rng.normal(0, 0.1)
        p = np.diff(np.concatenate([[0.0], _cdf(LE, mc, sigma), [1.0]]))
        if tail:
            ps = np.diff(np.concatenate([[0.0], _cdf(LE, mc + math.log(5), sigma), [1.0]]))
            p = (1 - tail) * p + tail * ps
        counts += rng.multinomial(rng.poisson(n / cols), p / p.sum())
    keep = counts > 0
    return Hist(j, 0, 0, LO[keep], HI[keep], counts[keep])


def scenario(seed: int, k: int, n: float, jitter: float, tail: float = 0.0):
    rng = np.random.default_rng(seed)
    cycles = [window_hist(rng, j, n, jitter) for j in range(1, k + 1)]
    now = window_hist(rng, 0, n, jitter, tail)
    return now, cycles


def run(seed: int, k: int, n: float, jitter: float, tail: float = 0.0):
    now, cycles = scenario(seed, k, n, jitter, tail)
    edges = common_edges([now, *cycles])
    x, _ = default_threshold(cycles, edges)
    return compare_tail(now, cycles, x, "1w")


ROWS = [  # k, n per window, cycle jitter of the log-median
    (3, 2_000, 0.05), (4, 2_000, 0.05), (4, 50_000, 0.05), (4, 50_000, 0.0),
    (7, 2_000, 0.05), (7, 50_000, 0.10), (4, 300, 0.05),
]  # fmt: skip


def table(seeds: int) -> None:
    print()
    print("| k | n/window | jitter | share 90% PI coverage | false alarms | detect 2% x5 slower | detect 5% x5 slower |")  # fmt: skip
    print("|---|---|---|---|---|---|---|")
    for k, n, jit in ROWS:
        cov = fa = d2 = d5 = 0
        for s in range(seeds):
            c = run(40_000 + s, k, n, jit)
            cov += c.share.normal[0] <= c.share.value <= c.share.normal[1]
            fa += c.verdict == "unusual"
            d2 += run(50_000 + s, k, n, jit, 0.02).direction == "higher"
            d5 += run(60_000 + s, k, n, jit, 0.05).direction == "higher"
        print(f"| {k} | {n} | {jit:.0%} | {cov / seeds:.3f} | {100 * fa / seeds:.1f}% | {100 * d2 / seeds:.0f}% | {100 * d5 / seeds:.0f}% |")  # fmt: skip
