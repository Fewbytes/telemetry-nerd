"""Seeded discrete-event M/M/c simulation scraped like Prometheus (Little's law tests, czt.2).

Per instance: Poisson arrivals (piecewise-constant rate), c servers, exponential service, FCFS.
The latency timer starts at arrival (consistent with the in-flight gauge) or at service start
(queueing before the timer: unmeasured). Every `scrape_s` the counter, the histogram _sum/_count
and the in-flight gauge are read; sub-step quantities are their per-second increases / samples.
"""

from __future__ import annotations

import heapq

import numpy as np

from telemetry_nerd.analysis.littles import Substeps


def simulate(
    seed: int,
    duration_s: float = 3600.0,
    rates: list[tuple[float, float]] | None = None,  # (from_s, arrivals/s), piecewise constant
    mu: float = 1.0,
    c: int = 4,
    scrape_s: float = 15.0,
    timer: str = "arrival",  # arrival | service_start
) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    rates = rates or [(0.0, 2.0)]
    edges = [r[0] for r in rates] + [duration_s]
    arr: list[np.ndarray] = []
    for (a, lam), b in zip(rates, edges[1:], strict=True):
        n = rng.poisson(lam * (b - a))
        arr.append(np.sort(rng.uniform(a, b, n)))
    t_arr = np.concatenate(arr)
    service = rng.exponential(1 / mu, t_arr.size)
    free = [0.0] * c
    start = np.empty_like(t_arr)
    for i, t in enumerate(t_arr):
        f = heapq.heappop(free)
        st = max(f, t)
        start[i] = st
        heapq.heappush(free, st + service[i])
    depart = start + service
    measured = depart - (t_arr if timer == "arrival" else start)
    ts = np.arange(scrape_s, duration_s + 1e-9, scrape_s)
    order = np.argsort(depart)
    dep_sorted, lat_sorted = depart[order], np.cumsum(measured[order])
    counter = np.searchsorted(t_arr, ts, side="right").astype(float)
    done = np.searchsorted(dep_sorted, ts, side="right")
    hsum = np.where(done > 0, lat_sorted[np.maximum(done - 1, 0)], 0.0)
    gauge = counter - done  # arrived and not yet departed at the scrape instant
    return {"ts": ts, "counter": counter, "count": done.astype(float), "sum": hsum, "gauge": gauge}


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
                 latency_name: str = LATENCY) -> None:  # fmt: skip
        self.name = "default"
        self.identity = "sim"
        self.resolution_ms = int(scrape_s * 1000)
        self.sims, self.start_ms, self.scrape_s = sims, start_ms, scrape_s
        self.hide, self.latency_scale, self.latency_name = hide, latency_scale, latency_name
        self.exprs: list[str] = []

    async def probe(self) -> dict:
        return {"reachable": True}

    async def scrape_interval(self, selector: str) -> int | None:
        return self.resolution_ms

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
            v = x if role == "gauge" else np.diff(np.r_[0.0, x]) / self.scrape_s
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
