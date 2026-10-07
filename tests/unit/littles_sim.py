"""Seeded discrete-event M/M/c simulation scraped like Prometheus (Little's law tests, czt.2).

Per instance: Poisson arrivals (piecewise-constant rate), c servers, exponential service, FCFS.
The latency timer starts at arrival (consistent with the in-flight gauge) or at service start
(queueing before the timer: unmeasured). Every `scrape_s` the counter, the histogram _sum/_count
and the in-flight gauge are read; sub-step quantities are their per-second increases / samples.
"""

from __future__ import annotations

import heapq
import math

import numpy as np

from telemetry_nerd.analysis.littles import Substeps


def _requests(
    seed: int,
    duration_s: float,
    rates: list[tuple[float, float]],
    mu: float,
    c: int,
    batch: float,
    service_cv: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The seeded per-request arrival/service-start/departure times (shared by `simulate` and
    `exact_r`, so scraping or re-grading a window never perturbs the realised path it is graded
    against)."""
    rng = np.random.default_rng(seed)
    edges = [r[0] for r in rates] + [duration_s]
    arr: list[np.ndarray] = []
    for (a, lam), b in zip(rates, edges[1:], strict=True):
        n = rng.poisson(lam * (b - a) / batch)
        t = np.sort(rng.uniform(a, b, n))
        if batch > 1:  # overdispersed arrivals: index of dispersion 2 batch - 1
            t = np.repeat(t, rng.geometric(1 / batch, n))
        arr.append(t)
    t_arr = np.concatenate(arr)
    if service_cv == 1.0:
        service = rng.exponential(1 / mu, t_arr.size)
    else:
        s2 = math.log(1 + service_cv**2)
        service = rng.lognormal(-math.log(mu) - s2 / 2, math.sqrt(s2), t_arr.size)
    free = [0.0] * c
    start = np.empty_like(t_arr)
    for i, t in enumerate(t_arr):
        f = heapq.heappop(free)
        st = max(f, t)
        start[i] = st
        heapq.heappush(free, st + service[i])
    depart = start + service
    return t_arr, start, depart


def simulate(
    seed: int,
    duration_s: float = 3600.0,
    rates: list[tuple[float, float]] | None = None,  # (from_s, arrivals/s), piecewise constant
    mu: float = 1.0,
    c: int = 4,
    scrape_s: float = 15.0,
    timer: str = "arrival",  # arrival | service_start
    counter: str = "arrivals",  # arrivals | completions: when the request counter increments
    batch: float = 1.0,  # mean arrivals per batch (geometric sizes, one instant): clustered
    service_cv: float = 1.0,  # service-time CV (1: exponential; else lognormal, mean 1 / mu)
    tile_phase_s: float = 0.0,  # counter grid offset from the gauge/tile grid (czt.2, p1ht)
    tile_jitter_s: float = 0.0,  # +-independent per-tile scrape jitter around that grid (p1ht)
) -> dict[str, np.ndarray]:
    """`tile_phase_s`/`tile_jitter_s`: the raw scrape that rate()/increase() actually extrapolate
    from is not guaranteed to land exactly on the nominal tile edge (0/0: a VM `increase(x[tile])`
    tile, exact, as before). `tile_phase_s` is a constant offset (the whole counter grid shifted,
    as from a query step that is not in phase with the real scrape cadence); `tile_jitter_s` is an
    independent +-uniform offset drawn per tile (real per-scrape timing jitter, as in
    tests/integration/test_queue_sim_vm.py's `rng.randint(-150, 150)`). The gauge is still an
    instant reading at the nominal tile edge itself (its own query, unaffected). A spike whose rate
    changes inside the fragment this leaves or borrows is split across tiles at a phase the window
    grid never sees, which is exactly what term (3) (`sd_count`) has to bound."""
    rates = rates or [(0.0, 2.0)]
    t_arr, start, depart = _requests(seed, duration_s, rates, mu, c, batch, service_cv)
    measured = depart - (t_arr if timer == "arrival" else start)
    ts = np.arange(scrape_s, duration_s + 1e-9, scrape_s)
    order = np.argsort(depart)
    dep_sorted, lat_sorted = depart[order], np.cumsum(measured[order])
    arrived_g = np.searchsorted(t_arr, ts, side="right").astype(float)
    done_g = np.searchsorted(dep_sorted, ts, side="right")
    gauge = arrived_g - done_g  # arrived and not yet departed at the (unshifted) tile edge
    cts = ts - tile_phase_s
    if tile_jitter_s > 0:
        jrng = np.random.default_rng([seed, 0x7AD11E])
        cts = cts + jrng.uniform(-tile_jitter_s, tile_jitter_s, cts.size)
    cts = np.clip(cts, 0.0, duration_s)
    arrived = np.searchsorted(t_arr, cts, side="right").astype(float)
    done = np.searchsorted(dep_sorted, cts, side="right")
    hsum = np.where(done > 0, lat_sorted[np.maximum(done - 1, 0)], 0.0)
    assert counter in ("arrivals", "completions")
    req = arrived if counter == "arrivals" else done.astype(float)
    return {"ts": ts, "counter": req, "count": done.astype(float), "sum": hsum, "gauge": gauge}


def exact_r(
    seed: int,
    start_s: float,
    end_s: float,
    duration_s: float = 3600.0,
    rates: list[tuple[float, float]] | None = None,
    mu: float = 1.0,
    c: int = 4,
    timer: str = "arrival",
    batch: float = 1.0,
    service_cv: float = 1.0,
) -> float:
    """The simulation's exact R = L / (lambda W) over [start_s, end_s), from the continuous
    (unscraped) path: L the time-average of true concurrency (always since physical arrival,
    whichever timer measures W), lambda W the arrival rate times the mean measured latency of
    requests completing in the window -- the same estimand a judged window's ci95 claims to cover
    (tests/integration/test_queue_sim_vm.py's `exact_windows`, same idea, for this simulator)."""
    rates = rates or [(0.0, 2.0)]
    t_arr, start_t, depart = _requests(seed, duration_s, rates, mu, c, batch, service_cv)
    T = end_s - start_s
    overlap = np.clip(np.minimum(depart, end_s) - np.maximum(t_arr, start_s), 0.0, None)
    L = float(overlap.sum()) / T
    lam = float(np.count_nonzero((t_arr >= start_s) & (t_arr < end_s))) / T
    done = (depart >= start_s) & (depart < end_s)
    if not np.any(done):
        return math.nan
    measured = depart - (t_arr if timer == "arrival" else start_t)
    W = float(measured[done].mean())
    lw = lam * W
    return L / lw if lw > 0 else math.nan


def substeps(
    m: dict[str, np.ndarray], scrape_s: float = 15.0, drop_gauge: bool = False
) -> Substeps:
    d = lambda x: np.diff(np.r_[0.0, x]) / scrape_s
    ts_ms = (m["ts"] * 1000).astype(np.int64)
    n = ts_ms.size
    conc = np.full(n, np.nan) if drop_gauge else m["gauge"].astype(float)
    return Substeps(
        ts_ms, d(m["counter"]), d(m["sum"]), d(m["count"]), conc,
        np.zeros(n) if drop_gauge else np.ones(n), int(scrape_s * 1000),
    )  # fmt: skip


ARRIVALS = "http_requests_total"
LATENCY = "http_request_duration_seconds"
CONCURRENCY = "http_server_active_requests"


class SimSource:
    """A source serving simulated instances for the four Little's law queries at the scrape step.

    Bucket ts = start_ms + 15 s * k is sim scrape k (sim time 15 s * k). Counters are served as
    their per-second increase over the bucket, the gauge as its sample; `sum by (instance)` keeps
    instances apart, `sum` adds them up. `hide` = {(role, instance)} the source does not have."""

    semantics = None

    def __init__(self, sims: dict[str, dict], start_ms: int, scrape_s: float = 15.0,
                 hide: set[tuple[str, str]] = frozenset(), latency_scale: float = 1.0,
                 latency_name: str = LATENCY, flavor: str | None = None,
                 scrape_ms: int | None = None) -> None:  # fmt: skip
        self.name = "default"
        self.identity = "sim"
        self.resolution_ms = int(scrape_s * 1000)
        self.sims, self.start_ms, self.scrape_s = sims, start_ms, scrape_s
        self.hide, self.latency_scale, self.latency_name = hide, latency_scale, latency_name
        self.exprs: list[str] = []
        self.discovery = None
        if flavor:  # VictoriaMetrics: counters are queried as increase() per scrape tile
            self.flavor = flavor
        self.scrape_ms = scrape_ms if scrape_ms is not None else self.resolution_ms
        self.probes: list[tuple[str, int | None]] = []

    async def probe(self) -> dict:
        return {"reachable": True}

    async def scrape_interval(self, selector: str, at_ms: int | None = None) -> int | None:
        self.probes.append((selector, at_ms))
        return self.scrape_ms

    def _role(self, expr: str) -> str:
        if f"{self.latency_name}_sum" in expr:
            return "sum"
        if f"{self.latency_name}_count" in expr:
            return "count"
        if CONCURRENCY in expr:
            return "gauge"
        return "counter"

    async def fetch(self, expr, rng, step_ms):
        import pyarrow as pa

        from telemetry_nerd.model.series import (
            BUCKET_SCHEMA,
            SERIES_SCHEMA,
            FetchResult,
            labels_json,
            series_id,
        )

        assert step_ms == self.resolution_ms, "the sim serves the scrape step only"
        self.exprs.append(expr)
        role = self._role(expr)
        grouped = "by (instance)" in expr
        per: dict[str, np.ndarray] = {}
        for inst, m in self.sims.items():
            if (role if role in ("gauge", "counter") else "latency", inst) in self.hide:
                continue
            x = m[role]
            div = 1.0 if "increase(" in expr else self.scrape_s  # increase(): per tile
            v = x if role == "gauge" else np.diff(np.r_[0.0, x]) / div
            if role == "sum":
                v = v * self.latency_scale
            per[inst] = v
        if not grouped and per:
            per = {"": np.sum(list(per.values()), axis=0)}
        rows = []
        labels = {}
        for inst, v in per.items():
            lb = {"instance": inst} if grouped else {}
            sid = series_id(self.name, lb)
            labels[sid] = lb
            for k, val in enumerate(v):
                ts = self.start_ms + int((k + 1) * self.scrape_s * 1000)
                if rng.start_ms <= ts <= rng.end_ms:
                    rows.append((ts, sid, float(val)))
        vals = [r[2] for r in rows]
        buckets = pa.table(
            {"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows], "avg": vals,
             "min": vals, "max": vals, "count": [1] * len(rows)},
            schema=BUCKET_SCHEMA,
        )  # fmt: skip
        series = pa.table(
            {"series_id": list(labels), "labels": [labels_json(lb) for lb in labels.values()]},
            schema=SERIES_SCHEMA,
        )
        return FetchResult(buckets, series)

    async def discover(self):
        return self.discovery


class EsSimSource(SimSource):
    """The arrival and latency signals as an Elasticsearch access-log index serves them: the
    rate form (documents per second) and the stats form (mean latency and count of the requests
    completing in each query bucket), from the same simulation as SimSource."""

    query_language = "es_dsl"

    def __init__(self, sims: dict[str, dict], start_ms: int, **kw) -> None:
        super().__init__(sims, start_ms, **kw)
        self.identity = "es-sim"

    async def fetch(self, expr, rng, step_ms):
        import pyarrow as pa

        from telemetry_nerd.model.series import (
            BUCKET_SCHEMA,
            SERIES_SCHEMA,
            FetchResult,
            labels_json,
            series_id,
        )

        assert step_ms == self.resolution_ms, "the sim serves the scrape step only"
        self.exprs.append(expr)
        stats = '"stats"' in expr
        d = lambda key: sum(np.diff(np.r_[0.0, m[key]]) for m in self.sims.values())
        count = d("count")
        values = d("sum") / np.where(count > 0, count, 1) if stats else d("counter") / self.scrape_s
        sid = series_id(self.name, {})
        rows = []
        for k in range(count.size):
            ts = self.start_ms + int((k + 1) * self.scrape_s * 1000)
            if not rng.start_ms <= ts <= rng.end_ms or (stats and count[k] == 0):
                continue
            rows.append((ts, float(values[k]), int(count[k]) if stats else 1))
        vals = [r[1] for r in rows]
        buckets = pa.table(
            {"ts_ms": [r[0] for r in rows], "series_id": [sid] * len(rows), "avg": vals,
             "min": vals, "max": vals, "count": [r[2] for r in rows]},
            schema=BUCKET_SCHEMA,
        )  # fmt: skip
        series = pa.table({"series_id": [sid] if rows else [], "labels": [labels_json({})] if rows else []},
                          schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)
