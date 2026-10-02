"""Synthetic series for development and tests (known ground truth)."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterable

import httpx
import pyarrow as pa

from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)


def exposition(metric: str, labels: dict[str, str], samples: Iterable[tuple[int, float]]) -> str:
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    head = f"{metric}{{{inner}}}" if inner else metric
    return "".join(f"{head} {value!r} {ts}\n" for ts, value in samples)


def push(base_url: str, text: str, attempts: int = 40) -> None:
    """Import exposition text and flush so it is immediately queryable.
    Retries connection errors so it works right after `podman compose up`."""
    with httpx.Client(timeout=60) as client:
        for attempt in range(attempts):
            try:
                client.post(
                    f"{base_url}/api/v1/import/prometheus", content=text.encode()
                ).raise_for_status()
                break
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5)
        client.get(f"{base_url}/internal/force_flush").raise_for_status()


LATENCY_LE = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _lognormal_cdf(x: float, median: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf(math.log(x / median) / (sigma * math.sqrt(2))))


def histogram_text(
    metric: str, labels: dict[str, str], scrapes: list[tuple[int, int, float]], sigma: float = 0.6
) -> str:
    """Classic cumulative _bucket counters. scrapes: (ts, requests since last scrape, median
    latency). Deterministic: per-bucket counts are rounded expectations, so cumulative
    counts never decrease across le."""
    edges = (*LATENCY_LE, math.inf)
    cum = dict.fromkeys(edges, 0)
    samples: dict[float, list[tuple[int, float]]] = {e: [] for e in edges}
    for ts, k, median in scrapes:
        for e in edges:
            cum[e] += k if math.isinf(e) else round(k * _lognormal_cdf(e, median, sigma))
            samples[e].append((ts, float(cum[e])))
    return "".join(
        exposition(
            f"{metric}_bucket", {**labels, "le": "+Inf" if math.isinf(e) else f"{e:g}"}, samples[e]
        )
        for e in edges
    )


GAPPY_HOLE_PERIOD_MS = 3 * 3_600_000
GAPPY_HOLE_OFFSET_MS = 3_600_000
GAPPY_HOLE_MS = 15 * 60_000


def demo_text(start_ms: int, end_ms: int, interval_ms: int = 15_000, seed: int = 7) -> str:
    """Latency gauge (hourly sine + noise) for instances a,b,c; c spikes to 1.5
    for 5 minutes starting at 2/3 of the range. Request counter per instance."""
    rng = random.Random(seed)
    spike_start = start_ms + (end_ms - start_ms) * 2 // 3
    spike_start -= spike_start % interval_ms
    spike_end = spike_start + 5 * 60_000
    parts: list[str] = []
    for instance in ("a", "b", "c"):
        latency: list[tuple[int, float]] = []
        requests: list[tuple[int, float]] = []
        scrapes: list[tuple[int, int, float]] = []
        total = 0.0
        for ts in range(start_ms, end_ms, interval_ms):
            if instance == "c" and spike_start <= ts < spike_end:
                value = 1.5
            else:
                phase = 2 * math.pi * (ts % 3_600_000) / 3_600_000
                value = round(0.05 + 0.02 * math.sin(phase) + rng.uniform(0, 0.01), 6)
            latency.append((ts, value))
            increment = rng.randint(600, 900)
            total += increment
            requests.append((ts, total))
            scrapes.append((ts, increment, value))
        parts.append(exposition("tn_demo_latency_seconds", {"instance": instance}, latency))
        parts.append(exposition("tn_demo_requests_total", {"instance": instance}, requests))
        parts.append(
            histogram_text("tn_demo_request_duration_seconds", {"instance": instance}, scrapes)
        )
    # d's 15-minute hole is anchored to wall-clock time (one per 3h block): seeds overlap in the
    # persistent dev VM, and a hole placed relative to each seed's range is filled by the next.
    # (Renamed from tn_demo_gappy_seconds: old seeds left that series hole-free in dev VMs.)
    hole = range(GAPPY_HOLE_OFFSET_MS, GAPPY_HOLE_OFFSET_MS + GAPPY_HOLE_MS)
    born = start_ms + (end_ms - start_ms) // 2
    born -= born % interval_ms
    gappy_d = [
        (ts, 0.05)
        for ts in range(start_ms, end_ms, interval_ms)
        if ts % GAPPY_HOLE_PERIOD_MS not in hole
    ]
    gappy_e = [(ts, 0.07) for ts in range(born, end_ms, interval_ms)]
    parts.append(exposition("tn_demo_holes_seconds", {"instance": "d"}, gappy_d))
    parts.append(exposition("tn_demo_holes_seconds", {"instance": "e"}, gappy_e))
    return "".join(parts)


def periodic_buckets(
    start_ms,
    end_ms,
    step_ms,
    components,
    *,
    base=0.0,
    noise=0.0,
    seed=0,
    gaps=(),
    labels=None,
    source="synthetic",
) -> FetchResult:
    """Ground truth for spectrum/filter tests. components: (period_ms, amplitude, onset_ms|None).
    gaps: [(t0, t1)) with no rows at all (never zero-filled)."""
    rnd = random.Random(seed)
    lb = labels or {"job": "synthetic"}
    sid = series_id(source, lb)
    ts, vals = [], []
    for t in range(start_ms, end_ms + 1, step_ms):
        if any(a <= t < b for a, b in gaps):
            continue
        v = base + rnd.gauss(0, noise) if noise else base
        for period, amp, onset in components:
            if onset is None or t >= onset:
                v += amp * math.sin(2 * math.pi * t / period)
        ts.append(t)
        vals.append(v)
    n = len(ts)
    table = pa.table(
        {
            "ts_ms": ts,
            "series_id": [sid] * n,
            "avg": vals,
            "min": vals,
            "max": vals,
            "count": [1] * n,
        },
        schema=BUCKET_SCHEMA,
    )
    return FetchResult(
        table, pa.table({"series_id": [sid], "labels": [labels_json(lb)]}, schema=SERIES_SCHEMA)
    )
