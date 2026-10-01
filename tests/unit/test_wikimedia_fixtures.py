"""PromQLSource against recorded Wikimedia Thanos responses: no network."""

from pathlib import Path

import httpx
import pytest

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.presets import WIKIMEDIA
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import ReplayTransport

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "wikimedia"
START_MS = 1_790_000_000_000 // 3_600_000 * 3_600_000  # as in scripts/record_wikimedia.py
RANGE = TimeRange(START_MS, START_MS + 3_600_000)


@pytest.fixture
def source():
    client = httpx.AsyncClient(transport=ReplayTransport(FIXTURES))
    return PromQLSource.from_spec(WIKIMEDIA, client=client)


def test_wikimedia_preset_is_polite_and_prometheus_flavored():
    assert WIKIMEDIA.flavor == "prometheus"
    assert WIKIMEDIA.politeness.max_concurrency == 1
    assert WIKIMEDIA.politeness.min_interval_ms >= 1000


async def test_selector_fetch_returns_buckets_for_one_series(source):
    res = await source.fetch('node_load1{site="eqiad",instance="wdqs1018:9100"}', RANGE, 60_000)
    assert res.series.num_rows == 1
    assert res.buckets.num_rows > 0
    assert res.partial == 0


async def test_expression_fetch_from_fixtures(source):
    expr = 'sum by (site) (rate(node_network_receive_bytes_total{device="eth0",site="eqiad"}[5m]))'
    res = await source.fetch(expr, RANGE, 60_000)
    assert res.series.num_rows == 1
    assert res.buckets.num_rows > 0


async def test_unrecorded_query_cannot_reach_the_network(source):
    from telemetry_nerd.sources.base import SourceUnavailable

    with pytest.raises(SourceUnavailable):
        await source.fetch("up", RANGE, 60_000)
