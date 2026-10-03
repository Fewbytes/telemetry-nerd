"""Discover and connect real Grafana datasources over the network (slow, polite).

Run: just test-integration -k grafana
Exercises the acceptance criteria directly: list play.grafana.org/grafana.wikimedia.org
datasources, connect the Prometheus one by uid, and check the detected flavor.
"""

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.grafana import discover_datasources, probe_backend
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration


async def test_play_mimir_datasource_detected_as_prometheus_flavor():
    datasources = await discover_datasources("https://play.grafana.org")
    mimir = next(d for d in datasources if d.uid == "grafanacloud-prom")
    assert mimir.supported is True
    backend, flavor = await probe_backend(mimir.proxy_url("https://play.grafana.org"))
    assert backend == "mimir" and flavor == "prometheus"


async def test_wikimedia_thanos_datasource_connects(tmp_path):
    datasources = await discover_datasources("https://grafana.wikimedia.org")
    thanos = next(d for d in datasources if d.uid == "000000026")
    assert thanos.supported is True
    svc = build_service(Settings(data_dir=tmp_path, source_url="http://127.0.0.1:9"))
    try:
        spec = SourceSpec.model_validate(
            {
                "name": "wikimedia-via-grafana",
                "url": thanos.proxy_url("https://grafana.wikimedia.org"),
                "politeness": {"max_concurrency": 1, "min_interval_ms": 1000, "timeout_s": 90},
            }
        )
        out = await svc.source_connect(spec)
    finally:
        if "wikimedia-via-grafana" in svc.sources:
            await svc.source_disconnect("wikimedia-via-grafana")
    assert out["status"]["reachable"] is True
