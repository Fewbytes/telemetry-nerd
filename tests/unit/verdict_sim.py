"""Seeded synthetic RED / USE windows for binding verdicts (bead czt.4).

Cycle 0 is now, cycles 1..k the reference windows. 1-min steps. Requests ~50/s with a per-cycle
level jitter and AR(1) noise; an error share of 0.2% with per-cycle jitter and AR(1)
overdispersion; lognormal latency (median 80 ms, sigma 0.5) with a jittered median, read as the
share of requests above a fixed edge (~p95); utilization ~0.4 and a queue ~2 with AR(1) noise.
Scenarios inject a change into cycle 0 only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np

STEP_MS = 60_000
N = 60
K = 4
T0 = 1_800_000_000_000  # now's first step end
X_SLOW = 0.08 * math.exp(1.645 * 0.5)  # the reference's ~p95
_N = NormalDist()


def _ar(rng: np.random.Generator, n: int, phi: float, sd: float) -> np.ndarray:
    e = rng.normal(0, sd * math.sqrt(1 - phi * phi), n)
    x = np.empty(n)
    x[0] = rng.normal(0, sd)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


@dataclass
class Window:
    requests: np.ndarray
    errors: np.ndarray
    slow: np.ndarray
    rate: np.ndarray  # requests per second
    util: np.ndarray
    queue: np.ndarray
    median_s: np.ndarray = field(repr=False)


@dataclass
class Scenario:
    error_burst: tuple[int, int] | None = None  # [first, last] step, x10 errors
    latency_shift: int | None = None  # from this step, median x1.6
    saturation: tuple[int, int] | None = None  # util -> 0.97, queue x5
    rate_drop: int | None = None  # from this step, requests x0.6
    error_factor: float = 10.0
    latency_factor: float = 1.6
    error_share: float = 0.002
    #: errors in clusters (e.g. retries of one request): mean cluster size (geometric sizes,
    #: negative-binomial counts), drawn per window lognormal with this log-sd (heterogeneous
    #: clustering across windows); None: independent errors
    error_cluster: float | None = None
    cluster_sd: float = 0.0


def window(rng: np.random.Generator, sc: Scenario | None = None, n: int = N) -> Window:
    sc = sc or Scenario()
    lam = 50 * math.exp(rng.normal(0, 0.05)) * np.exp(_ar(rng, n, 0.6, 0.03))
    if sc.rate_drop is not None:
        lam[sc.rate_drop :] *= 0.6
    req = rng.poisson(lam * 60).astype(float)
    p = sc.error_share * math.exp(rng.normal(0, 0.1)) * np.exp(_ar(rng, n, 0.5, 0.2))
    if sc.error_burst:
        a, b = sc.error_burst
        p[a : b + 1] *= sc.error_factor
    if sc.error_cluster is None:
        err = rng.binomial(req.astype(int), np.clip(p, 0, 1)).astype(float)
    else:
        size = max(1.0, sc.error_cluster * math.exp(rng.normal(0, sc.cluster_sd)))
        k = rng.binomial(req.astype(int), np.clip(p / size, 0, 1))
        extra = rng.negative_binomial(np.maximum(k, 1), 1 / size) * (k > 0)
        err = np.minimum(k + extra, req).astype(float)
    med = 0.08 * math.exp(rng.normal(0, 0.03)) * np.exp(_ar(rng, n, 0.6, 0.04))
    if sc.latency_shift is not None:
        med[sc.latency_shift :] *= sc.latency_factor
    p_slow = np.array([1 - _N.cdf((math.log(X_SLOW) - math.log(m)) / 0.5) for m in med])
    slow = rng.binomial(req.astype(int), p_slow).astype(float)
    util = 0.4 + rng.normal(0, 0.03) + _ar(rng, n, 0.7, 0.03)
    queue = 2 * math.exp(rng.normal(0, 0.1)) * np.exp(_ar(rng, n, 0.7, 0.15))
    if sc.saturation:
        a, b = sc.saturation
        util[a : b + 1] = 0.97 + rng.normal(0, 0.01, b + 1 - a)
        queue[a + 2 : b + 1] *= 5
    return Window(req, err, slow, req / 60, np.clip(util, 0, 1), queue, med)


def cycles(
    seed: int, sc: Scenario | None = None, k: int = K, null: Scenario | None = None
) -> list[Window]:
    """Now (`sc`) and k reference windows (`null`: the in-control process, default)."""
    rng = np.random.default_rng(seed)
    return [window(rng, sc)] + [window(rng, null) for _ in range(k)]


def ts(n: int = N) -> np.ndarray:
    return T0 + np.arange(n, dtype=np.int64) * STEP_MS


LOOKBACK_MS = 120_000  # rate() over 3 steps of 1 min: half its excess


def red_inputs(cs: list[Window]):
    from telemetry_nerd.analysis.verdicts import RoleInput

    now, refs = cs[0], cs[1:]
    return {
        "rate": RoleInput("value", now.rate, [r.rate for r in refs], lookback_ms=LOOKBACK_MS),
        "errors": RoleInput(
            "share", (now.errors, now.requests), [(r.errors, r.requests) for r in refs],
            lookback_ms=LOOKBACK_MS,
        ),
        "duration": RoleInput(
            "share", (now.slow, now.requests), [(r.slow, r.requests) for r in refs],
            lookback_ms=LOOKBACK_MS,
        ),
    }  # fmt: skip


def use_inputs(cs: list[Window]):
    from telemetry_nerd.analysis.verdicts import RoleInput

    now, refs = cs[0], cs[1:]
    return {
        "utilization": RoleInput("value", now.util, [r.util for r in refs], bound=1.0),
        "saturation": RoleInput("value", now.queue, [r.queue for r in refs]),
    }
