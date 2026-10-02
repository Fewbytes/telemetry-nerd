"""Live: operating profiles through the wikimedia -> wikimedia-1h pairing (bead 2as.23).

Run: just test-integration -k wikimedia_profile   (~10 s, ~8 narrow requests, 1 at a time,
>= 1 s apart: interactive volume only, Wikimedia's robots.txt disallows crawling).
"""

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.presets import WIKIMEDIA, WIKIMEDIA_1H
from telemetry_nerd.sources.promql import PromQLSource

pytestmark = pytest.mark.integration

GAUGE = 'node_load1{site="eqiad",instance="wdqs1018:9100"}'


async def test_gauge_profile_from_the_paired_1h_datasource(tmp_path):
    svc = build_service(Settings(data_dir=tmp_path, source_url="http://127.0.0.1:9"))
    for spec in (WIKIMEDIA, WIKIMEDIA_1H):
        svc.sources.add(spec, PromQLSource.from_spec(spec))
    try:
        p = await svc.profiles.ensure("wikimedia", GAUGE)
    finally:
        for spec in (WIKIMEDIA, WIKIMEDIA_1H):
            await svc.sources.get(spec.name).aclose()
    assert p.profiled_from == "wikimedia-1h" and p.kind == "level"
    assert p.series_total == 1 and p.pooled.n >= 700
    assert p.pooled.min <= p.pooled.p50 <= p.pooled.max
    assert p.pooled.envelope_hi >= p.pooled.p995
