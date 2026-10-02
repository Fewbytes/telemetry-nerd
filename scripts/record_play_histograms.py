"""Record Grafana Play histogram responses as offline test fixtures.

Four narrow queries through the polite source client (one at a time, spaced out).
Run: uv run python scripts/record_play_histograms.py
Replayed by tests/unit/test_play_fixtures.py, which uses the same windows.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.presets import PLAY
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import RecordingTransport

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "play"
NATIVE_SEL = 'traces_spanmetrics_latency{service="checkoutservice",span_kind="SPAN_KIND_SERVER"}'
NATIVE_RANGE = TimeRange(1_790_848_200_000, 1_790_850_000_000)  # 2026-10-01T09:50Z-10:20Z, 1m
CLASSIC_SEL = 'http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice"}'
CLASSIC_INF = (
    'sum(increase(http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice",'
    'le="+Inf"}[5m]))'
)
CLASSIC_RANGE = TimeRange(1_790_856_000_000, 1_790_859_600_000)  # 12:00Z-13:00Z, 5m


async def main() -> None:
    client = httpx.AsyncClient(transport=RecordingTransport(httpx.AsyncHTTPTransport(), OUT))
    src = PromQLSource.from_spec(PLAY, client=client)
    try:
        d = await src.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
        print(f"native: {d.scheme.describe()}; {d.series.num_rows} series, {d.rows.num_rows} cells")
        count_expr = f"sum by (cloud_region) (histogram_count(increase({NATIVE_SEL}[1m])))"
        v = await src.fetch_values(count_expr, NATIVE_RANGE, 60_000)
        print(f"native n: {v.buckets.num_rows} points")
        d = await src.fetch_histogram(CLASSIC_SEL, [], CLASSIC_RANGE, 300_000)
        print(
            f"classic: {d.scheme.describe()}; {d.series.num_rows} series, {d.rows.num_rows} cells"
        )
        v = await src.fetch_values(CLASSIC_INF, CLASSIC_RANGE, 300_000)
        print(f"classic n: {v.buckets.num_rows} points")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
