"""Record the Wikimedia operating-profile fixtures (bead 2as.23) and print what they show.

Runs ProfileService end to end against the live `wikimedia` -> `wikimedia-1h` pairing (the
paired source does the work) for a gauge, a counter rate and a histogram_quantile, 30 days x
1h, through the polite source clients (1 request at a time, >= 1 s apart, descriptive UA).
Every HTTP exchange is saved to tests/fixtures/wikimedia/profile/ together with clock.json
(the profile clock the offline test replays with). Values are rounded to 5 significant
digits to keep the fixture small. Interactive volume only: ~7 narrow requests per run.

Run: uv run python scripts/record_wikimedia_profiles.py     (network; CI never runs this)
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import httpx

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.presets import WIKIMEDIA, WIKIMEDIA_1H
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import RecordingTransport

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "wikimedia" / "profile"
HOUR_MS = 3_600_000
NODE = 'instance="wdqs1018:9100"'
EXPRS = {
    "gauge": f'node_load1{{site="eqiad",{NODE}}}',
    "counter": f'node_network_receive_bytes_total{{{NODE},device="eno12399np0"}}',
    "quantile": (
        "histogram_quantile(0.99, sum by (le) (rate("
        'envoy_cluster_upstream_rq_time_bucket{site="eqiad",prometheus="ops",'
        'instance="an-web1001:9631"}[$__rate_interval])))'
    ),
}


def _round(v: str, digits: int = 5) -> str:
    try:
        return f"{float(v):.{digits}g}"
    except ValueError:
        return v


class RoundingTransport(httpx.AsyncBaseTransport):
    """Shrink query_range matrices: round values and drop samples outside the profile window
    (the series cache fetches whole 720-bucket chunks, about twice the window)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, window: tuple[int, int]) -> None:
        self._inner = inner
        self._lo, self._hi = window

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        await resp.aread()
        if resp.status_code != 200 or not request.url.path.endswith("/query_range"):
            return resp
        body = json.loads(resp.content)
        for item in body.get("data", {}).get("result", []):
            item["values"] = [
                [t, _round(v)] for t, v in item["values"] if self._lo <= t <= self._hi
            ]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(body, separators=(",", ":")).encode(),
            request=request,
        )


async def main() -> None:
    for old in OUT.glob("*.json"):
        old.unlink()
    clock = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    window = (clock // 1000 - 30 * 24 * 3600, clock // 1000)
    transport = RecordingTransport(RoundingTransport(httpx.AsyncHTTPTransport(), window), OUT)
    client = httpx.AsyncClient(transport=transport)
    with tempfile.TemporaryDirectory() as tmp:
        svc = build_service(Settings(data_dir=Path(tmp), source_url="http://127.0.0.1:9"))
        svc.profiles.clock = lambda: clock
        for spec in (WIKIMEDIA, WIKIMEDIA_1H):
            svc.sources.add(spec, PromQLSource.from_spec(spec, client=client))
        try:
            for kind, expr in EXPRS.items():
                t0 = time.monotonic()
                p = await svc.profiles.ensure("wikimedia", expr)
                dt = time.monotonic() - t0
                print(
                    f"{kind}: {dt:.1f}s from={p.profiled_from} kind={p.kind} expr={p.expr[:70]}\n"
                    f"  series={p.series_total} n={p.pooled.n} caveats={p.caveats}\n"
                    f"  pooled={p.pooled.model_dump()}"
                )
        finally:
            await client.aclose()
    (OUT / "clock.json").write_text(
        json.dumps({"clock_ms": clock, "exprs": EXPRS}, indent=1) + "\n"
    )
    size = sum(f.stat().st_size for f in OUT.glob("*.json"))
    print(f"{len(list(OUT.glob('*.json')))} files, {size / 1024:.0f} KiB in {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
