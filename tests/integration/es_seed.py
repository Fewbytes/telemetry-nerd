"""A synthetic ECS-shaped access-log index with known per-minute counts and latencies."""

from __future__ import annotations

import json

import httpx

from telemetry_nerd.model.time import now_ms

INDEX = "tn-access-1"
PATTERN = "tn-access-*"
MINUTES = 30
GAP_MINUTE = 10  # no documents: an interior empty query bucket
MS = 1_000_000  # event.duration is in nanoseconds (ECS)

MAPPING = {"mappings": {"properties": {
    "@timestamp": {"type": "date"},
    "http": {"properties": {"response": {"properties": {"status_code": {"type": "long"}}}}},
    "event": {"properties": {"duration": {"type": "long", "meta": {"unit": "nanos"}}}},
    "service": {"properties": {"name": {"type": "keyword"}}},
    "url": {"properties": {"path": {"type": "keyword"}}},
    "message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
}}}  # fmt: skip


#: the series cache fetches in chunks of 720 query buckets (12 h at a 1 m step, aligned to the
#: epoch): a chunk edge next to GAP_MINUTE would make that interior zero a chunk's leading or
#: trailing bucket (no row). Seeding 1 h into a 12 h chunk keeps the 30 minutes in one chunk on
#: every run, so the tests never depend on the wall clock.
CHUNK_MS = 720 * 60_000


def base_ms() -> int:
    """Start of the first seeded minute: 1 h into the 12 h cache chunk that began 24-36 h ago."""
    return (now_ms() - 24 * 3_600_000) // CHUNK_MS * CHUNK_MS + 3_600_000


def _doc(ts: int, service: str, status: int, duration_ns: int, path: str) -> dict:
    return {"@timestamp": ts, "service": {"name": service},
            "http": {"response": {"status_code": status}}, "event": {"duration": duration_ns},
            "url": {"path": path}, "message": f"{service} {path} {status}"}  # fmt: skip


def docs(base: int) -> list[dict]:
    out = []
    for m in range(MINUTES):
        if m == GAP_MINUTE:
            continue
        t = base + m * 60_000
        out += [_doc(t + 500 + j * 1000, "checkout", 200, 40 * MS, "/cart") for j in range(10)]
        out += [_doc(t + 30_500 + j * 1000, "checkout", 500, 100 * MS, "/pay") for j in range(2)]
        out += [_doc(t + 45_500 + j * 1000, "search", 200, 20 * MS, "/q") for j in range(5)]
    return out


def seed(url: str, base: int) -> None:
    httpx.delete(f"{url}/{INDEX}", timeout=30)  # rerun-safe: 404 when absent
    httpx.put(f"{url}/{INDEX}", json=MAPPING, timeout=30).raise_for_status()
    lines = "".join(
        json.dumps({"index": {"_index": INDEX}}) + "\n" + json.dumps(d) + "\n" for d in docs(base)
    )
    r = httpx.post(f"{url}/_bulk?refresh=true", content=lines,
                   headers={"Content-Type": "application/x-ndjson"}, timeout=60)  # fmt: skip
    r.raise_for_status()
    assert not r.json()["errors"], r.json()
