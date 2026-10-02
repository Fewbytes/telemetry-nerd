"""ProfileService end to end on recorded Wikimedia responses: no network (bead 2as.23).

Fixtures: tests/fixtures/wikimedia/profile/ (scripts/record_wikimedia_profiles.py), 30 days x
1h of a gauge, a counter rate and a histogram_quantile, served by the paired `wikimedia-1h`
datasource. The recorded clock pins the profile window so the replayed requests match.
"""

import json
from pathlib import Path

import httpx
import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.presets import WIKIMEDIA, WIKIMEDIA_1H
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import ReplayTransport
from telemetry_nerd.sources.spec import Politeness

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "wikimedia" / "profile"
META = json.loads((FIXTURES / "clock.json").read_text())
EXPRS = META["exprs"]
HOUR_MS = 3_600_000


class NoNetwork(httpx.AsyncBaseTransport):
    """The unpaired `wikimedia` source must not be contacted for a profile."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.ConnectError("the paired profile source should serve this", request=request)


@pytest.fixture
def raw_transport():
    return NoNetwork()


@pytest.fixture
def svc(tmp_path, raw_transport):
    s = build_service(Settings(data_dir=tmp_path, source_url="http://127.0.0.1:9"))
    s.profiles.clock = lambda: META["clock_ms"]
    raw = httpx.AsyncClient(transport=raw_transport)
    down = httpx.AsyncClient(transport=ReplayTransport(FIXTURES))
    fast = Politeness(max_concurrency=1, min_interval_ms=0, timeout_s=90)  # replay: no spacing
    for spec, client in ((WIKIMEDIA, raw), (WIKIMEDIA_1H, down)):
        spec = spec.model_copy(update={"politeness": fast})
        s.sources.add(spec, PromQLSource.from_spec(spec, client=client))
    return s


async def test_gauge_profile_uses_the_paired_source_with_real_hourly_extremes(svc, raw_transport):
    p = await svc.profiles.ensure("wikimedia", EXPRS["gauge"])
    assert p.source == "wikimedia" and p.profiled_from == "wikimedia-1h"
    assert p.kind == "level" and p.expr == EXPRS["gauge"] and p.rate_window_ms is None
    assert p.series_total == 1 and p.pooled.n == 720 and p.caveats == []
    r = p.pooled
    # a load average: ordered range, sane magnitude, intra-hour extremes wider than hourly means
    assert r.min <= r.p005 <= r.p25 <= r.p50 <= r.p75 <= r.p995 <= r.max
    assert 40 < r.p50 < 70 and r.max < 200
    assert r.envelope_hi > r.p995 and r.envelope_lo < r.p005
    assert p.series[0].seasonal is not None  # 30 days of hourly data: a seasonal model is fit
    assert raw_transport.requests == []


async def test_counter_is_profiled_as_a_4h_rate_at_the_downsampled_resolution(svc):
    expr = EXPRS["counter"]
    p = await svc.profiles.ensure("wikimedia", expr)
    assert p.profiled_from == "wikimedia-1h" and p.kind == "rate"
    assert p.expr == f"rate({expr}[4h])" and p.rate_window_ms == 4 * HOUR_MS
    assert p.pooled.n == 720 and "no_intra_hour_extremes" in p.caveats
    assert 1e4 < p.pooled.p50 < 1e6  # bytes/s of a busy NIC, never the raw (huge) total
    assert p.pooled.min >= 0


async def test_histogram_quantile_is_a_series_of_hourly_quantiles(svc):
    p = await svc.profiles.ensure("wikimedia", EXPRS["quantile"])
    assert p.profiled_from == "wikimedia-1h" and p.kind == "quantile"
    assert "quantile_series" in p.caveats and p.pooled.n == 720
    assert p.expr.endswith("[4h])))")  # $__rate_interval resolved against the 1h resolution
    assert 0 < p.pooled.min <= p.pooled.p50 <= p.pooled.p995 <= p.pooled.max


async def test_profiles_are_stored_and_served_from_the_cache(svc, raw_transport):
    first = await svc.profiles.ensure("wikimedia", EXPRS["gauge"])
    cached = svc.profiles.cached("wikimedia", EXPRS["gauge"])
    assert cached is not None and cached.id == first.id and not cached.stale
    assert raw_transport.requests == []
