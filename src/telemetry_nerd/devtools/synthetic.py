"""Synthetic series for development and tests (known ground truth)."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterable

import httpx


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
        total = 0.0
        for ts in range(start_ms, end_ms, interval_ms):
            if instance == "c" and spike_start <= ts < spike_end:
                value = 1.5
            else:
                phase = 2 * math.pi * (ts % 3_600_000) / 3_600_000
                value = round(0.05 + 0.02 * math.sin(phase) + rng.uniform(0, 0.01), 6)
            latency.append((ts, value))
            total += rng.randint(600, 900)
            requests.append((ts, total))
        parts.append(exposition("tn_demo_latency_seconds", {"instance": instance}, latency))
        parts.append(exposition("tn_demo_requests_total", {"instance": instance}, requests))
    return "".join(parts)
