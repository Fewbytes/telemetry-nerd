"""Record small, trimmed fixtures of Grafana's own discovery endpoints (not a backend's
query API, which the public-sources fixtures already cover): /api/frontend/settings and
one datasource's /api/v1/status/buildinfo through its proxy, for grafana-front-door tests.

Trims /api/frontend/settings down to a handful of datasources (every Prometheus-type one,
plus a couple of non-Prometheus ones so the "unsupported" path has something to replay)
before saving, so the fixture stays small and does not carry Grafana Play's/Wikimedia's
full datasource list (dozens of unrelated cloud/IoT plugins).

Run: uv run python scripts/record_grafana_fixtures.py   (requires network; CI never runs this)
Writes tests/fixtures/grafana/<name>/.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from telemetry_nerd.sources.grafana import discover_datasources
from telemetry_nerd.sources.promql import USER_AGENT
from telemetry_nerd.sources.replay import RecordingTransport

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "grafana"
# (fixtures dir name, grafana url, uid of a Prometheus-compatible datasource to buildinfo-probe)
SOURCES = [
    ("play", "https://play.grafana.org", "grafanacloud-prom"),
    ("wikimedia", "https://grafana.wikimedia.org", "000000026"),
]
#: datasource names kept per Grafana instance (host): every Prometheus-compatible one worth
#: telling apart (a backend_hint, or none) plus a couple of non-Prometheus ones, so the
#: "unsupported" path has something to replay. Everything else is dropped from the fixture.
KEEP_NAMES = {
    "play.grafana.org": {"grafanacloud-ml-metrics", "grafanacloud-play-prom", "AWS IoT SiteWise"},
    "grafana.wikimedia.org": {
        "codfw prometheus/k8s",
        "thanos",
        "thanos-downsample-1h",
        "-- Grafana --",
    },
}


class TrimmingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, keep: set[str]) -> None:
        self._inner = inner
        self._keep = keep

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        await resp.aread()
        if resp.status_code != 200 or not request.url.path.endswith("/api/frontend/settings"):
            return resp
        full = json.loads(resp.content)
        ds = full.get("datasources")
        trimmed = {name: entry for name, entry in ds.items() if name in self._keep}
        # the full settings payload also carries Grafana's entire app/panel plugin catalog
        # (hundreds of KB, nothing this project reads): keep only what discover_datasources uses
        body = {"datasources": trimmed}
        return httpx.Response(
            resp.status_code,
            headers={"content-type": resp.headers.get("content-type", "application/json")},
            content=json.dumps(body, separators=(",", ":")).encode(),
            request=request,
        )


async def record(name: str, url: str, uid: str) -> None:
    out = ROOT / name
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    host = httpx.URL(url).host
    transport = RecordingTransport(
        TrimmingTransport(httpx.AsyncHTTPTransport(), KEEP_NAMES[host]), out
    )
    client = httpx.AsyncClient(transport=transport)
    try:
        datasources = await discover_datasources(url, client=client)
        print(f"{name}: {len(datasources)} datasources after trimming")
        proxy = f"{url.rstrip('/')}/api/datasources/proxy/uid/{uid}"
        resp = await client.get(
            f"{proxy}/api/v1/status/buildinfo", headers={"User-Agent": USER_AGENT}
        )
        print(f"{name}: buildinfo {resp.json().get('data')}")
    finally:
        await client.aclose()
    size = sum(f.stat().st_size for f in out.glob("*.json"))
    print(f"{name}: {len(list(out.glob('*.json')))} files, {size / 1024:.0f} KiB in {out}")


async def main() -> None:
    for name, url, uid in SOURCES:
        await record(name, url, uid)


if __name__ == "__main__":
    asyncio.run(main())
