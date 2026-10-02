"""Seeded synthetic fleets for fleet-analysis tests and calibration (bead lkn.3).

Every member follows the shared load L_t (a daily-like cycle) times exp(a_i + e_it): a_i is the
member's natural level (heterogeneity, sd `het`), e_it AR(1) noise (phi, marginal sd `sigma`;
`df` makes the innovations Student t). Planted faults are added on the log scale.
"""

from __future__ import annotations

import numpy as np

T = 288  # one day at 5m


def ar1(rng: np.random.Generator, m: int, t: int, phi: float, sigma: float, df: float | None):
    innov = (
        rng.standard_t(df, size=(m, t)) * np.sqrt((df - 2) / df) if df else rng.normal(size=(m, t))
    )
    e = np.empty((m, t))
    e[:, 0] = innov[:, 0]
    k = np.sqrt(1 - phi * phi)
    for j in range(1, t):
        e[:, j] = phi * e[:, j - 1] + k * innov[:, j]
    return sigma * e


def fleet(
    seed: int,
    m: int = 100,
    t: int = T,
    phi: float = 0.6,
    sigma: float = 0.1,
    het: float = 0.05,
    df: float | None = None,
    plant: bool = False,
    missing: float = 0.0,
) -> tuple[np.ndarray, dict[str, int]]:
    """Members x steps values; with `plant`, members 7 (persistent x1.8), 23 (transient: x2.7 for
    12 steps mid-window) and 61 (drifting: 0 -> x2.2 over the window). `missing`: share of
    member-steps dropped at random."""
    rng = np.random.default_rng(seed)
    steps = np.arange(t)
    load = 50 + 20 * np.sin(2 * np.pi * steps / T)
    logy = rng.normal(0, het, size=(m, 1)) + ar1(rng, m, t, phi, sigma, df)
    planted: dict[str, int] = {}
    if plant:
        planted = {"persistent": 7, "transient": 23 % m, "drifting": 61 % m}
        logy[planted["persistent"]] += 0.6
        mid = t // 2
        logy[planted["transient"], mid : mid + 12] += 1.0
        logy[planted["drifting"]] += 0.8 * steps / (t - 1)
    y = load * np.exp(logy)
    if missing:
        y[rng.random(y.shape) < missing] = np.nan
    return y, planted
