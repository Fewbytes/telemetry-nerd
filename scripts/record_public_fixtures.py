"""Record small, trimmed fixtures from the public registry sources (one per backend family).

Polite by construction: the registry's own politeness settings (1 request at a time,
>= 1 s apart, descriptive User-Agent), a fixed handful of narrow requests per source,
nothing broad. Responses are trimmed before they are saved so fixtures stay small:
metric names keep a fixed allowlist plus the first TAIL names, /metadata keeps entries for
the kept names only, /status/tsdb keeps the top few rows.

Run: uv run python scripts/record_public_fixtures.py [source ...]   (default: all with fixtures)
Writes tests/fixtures/<fixtures dir from public-sources.toml>/ including window.json (the
query window the tests replay). Requires network; CI never runs this.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.public import PUBLIC_SOURCES
from telemetry_nerd.sources.replay import RecordingTransport

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
TAIL = 150  # names kept besides the allowlist
TSDB_ROWS = 10
STEP_MS = 60_000
HOUR_MS = 3_600_000
KEEP = {
    "up",
    "node_load1",
    "node_cpu_seconds_total",
    "node_memory_MemTotal_bytes",
    "demo_api_request_duration_seconds_bucket",
    "demo_api_request_duration_seconds_count",
    "demo_api_request_duration_seconds_sum",
    "go_gc_pauses_seconds_bucket",
    "go_gc_pauses_seconds_count",
    "go_gc_pauses_seconds_sum",
    "alertmanager_http_request_duration_seconds_bucket",
    "alertmanager_http_request_duration_seconds_count",
    "alertmanager_http_request_duration_seconds_sum",
}
GAUGE_EXPR = "avg by (job) (node_load1)"
# source -> selector for fetch_histogram (classic `le`/vmrange family present on that source)
HISTOGRAMS = {
    "promlabs-demo": "demo_api_request_duration_seconds_bucket",
    "prometheus-demo": "alertmanager_http_request_duration_seconds_bucket",
    "cern-eos": "go_gc_pauses_seconds_bucket",
    "vm-playground": "alertmanager_http_request_duration_seconds_bucket",
}
DEFAULT = ["promlabs-demo", "prometheus-demo", "cern-eos", "cern-openstack", "vm-playground"]


class TrimmingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self._kept: set[str] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        await resp.aread()
        if resp.status_code != 200:
            return resp
        path = request.url.path
        body = json.loads(resp.content)
        data = body.get("data")
        if path.endswith("/label/__name__/values") and isinstance(data, list):
            tail = [n for n in sorted(data) if n not in KEEP][:TAIL]
            self._kept = {n for n in data if n in KEEP} | set(tail)
            body["data"] = sorted(self._kept)
        elif path.endswith("/metadata") and isinstance(data, dict) and self._kept is not None:
            body["data"] = {n: v[:1] for n, v in data.items() if n in self._kept}
        elif path.endswith("/status/tsdb") and isinstance(data, dict):
            body["data"] = {k: v[:TSDB_ROWS] if isinstance(v, list) else v for k, v in data.items()}
        else:
            return resp
        return httpx.Response(
            resp.status_code,
            headers={"content-type": resp.headers.get("content-type", "application/json")},
            content=json.dumps(body, separators=(",", ":")).encode(),
            request=request,
        )


async def record(name: str) -> None:
    entry = PUBLIC_SOURCES[name]
    if entry.fixtures is None:
        raise SystemExit(f"{name} has no fixtures dir in public-sources.toml")
    out = ROOT / entry.fixtures
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    end = int(time.time() * 1000) // HOUR_MS * HOUR_MS - HOUR_MS  # last full hour, aligned
    rng = TimeRange(end - HOUR_MS, end)
    (out / "window.json").write_text(
        json.dumps({"start_ms": rng.start_ms, "end_ms": rng.end_ms, "step_ms": STEP_MS}) + "\n"
    )
    transport = RecordingTransport(TrimmingTransport(httpx.AsyncHTTPTransport()), out)
    client = httpx.AsyncClient(transport=transport)
    src = PromQLSource.from_spec(entry.to_spec(), client=client)
    try:
        print(f"{name}: probe {await src.probe()}")
        d = await src.discover()
        print(f"{name}: {len(d.metrics)} names, coverage {d.metadata_coverage:.0%}, {d.caveats}")
        res = await src.fetch_values(GAUGE_EXPR, rng, STEP_MS)
        print(f"{name}: gauge {res.series.num_rows} series, {res.buckets.num_rows} buckets")
        if sel := HISTOGRAMS.get(name):
            dist = await src.fetch_histogram(sel, ["job"], rng, STEP_MS)
            print(f"{name}: histogram {sel} {dist.series.num_rows} series ({dist.scheme.kind})")
    finally:
        await client.aclose()
    size = sum(f.stat().st_size for f in out.glob("*.json"))
    print(f"{name}: {len(list(out.glob('*.json')))} files, {size / 1024:.0f} KiB in {out}")


async def main(names: list[str]) -> None:
    for name in names:
        await record(name)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or DEFAULT))
