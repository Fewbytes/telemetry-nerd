"""Record a small set of Wikimedia Thanos responses as offline test fixtures.

Interactive volume only (robots.txt disallows crawling): a handful of narrow queries
through the polite source client. Run: uv run python scripts/record_wikimedia.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.presets import WIKIMEDIA
from telemetry_nerd.sources.promql import USER_AGENT, PromQLSource
from telemetry_nerd.sources.replay import RecordingTransport

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "wikimedia"
# fixed, historical window: the same request is replayed by tests/unit/test_wikimedia_fixtures.py
START_MS = 1_790_000_000_000 // 3_600_000 * 3_600_000  # hour aligned
RANGE = TimeRange(START_MS, START_MS + 3_600_000)
STEP_MS = 60_000
QUERIES = [
    'node_load1{site="eqiad",instance="wdqs1018:9100"}',
    'sum by (site) (rate(node_network_receive_bytes_total{device="eth0",site="eqiad"}[5m]))',
]
METADATA = ["node_load1", "node_network_receive_bytes_total"]


async def main() -> None:
    client = httpx.AsyncClient(transport=RecordingTransport(httpx.AsyncHTTPTransport(), OUT))
    src = PromQLSource.from_spec(WIKIMEDIA, client=client)
    try:
        for expr in QUERIES:
            res = await src.fetch(expr, RANGE, STEP_MS)
            print(f"{expr}: {res.series.num_rows} series, {res.buckets.num_rows} buckets")
        for metric in METADATA:
            resp = await client.get(
                f"{WIKIMEDIA.url}/api/v1/metadata",
                params={"metric": metric},
                headers={"User-Agent": USER_AGENT},
                timeout=90,
            )
            print(f"metadata {metric}: HTTP {resp.status_code}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
