"""T0 learning against recorded Grafana Play responses (public OTel demo stack): no network.

Fixtures are a trimmed copy of one real discovery (scripts/learn_public.py play --record).
"""

from pathlib import Path

import httpx
import pytest

from telemetry_nerd.sources.presets import PLAY
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import ReplayTransport
from telemetry_nerd.sources.spec import Politeness

from .fakes import make_service

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "play"


@pytest.fixture
async def learned(tmp_path):
    client = httpx.AsyncClient(transport=ReplayTransport(FIXTURES))
    svc = make_service(tmp_path)
    # the preset's politeness spacing is for the live server; replays need none
    spec = PLAY.model_copy(update={"politeness": Politeness(min_interval_ms=0)})
    svc.sources.attach("play", PromQLSource.from_spec(spec, client=client), spec)
    out = await svc.learn("play")
    yield svc.ws, out
    await client.aclose()


def entry(ws, metric):
    return ws.catalog_entry("play", metric)


async def test_discovery_and_learning_summary(learned):
    _, out = learned
    assert out["metrics"] == 72 and out["new"] == 72 and out["complete"] is True
    assert any(c.startswith("metadata_coverage") for c in out["caveats"])
    assert "cardinality_unavailable" in out["caveats"]  # Grafana's proxy hides tsdb status


async def test_native_histograms_are_found_from_metadata(learned):
    ws, _ = learned
    e = entry(ws, "k6_browser_web_vital_cls")
    assert e.fields["histogram_family"].value == ["k6_browser_web_vital_cls"]
    assert e.fields["type"].value == "histogram" and e.fields["type"].origin == "metadata"


async def test_span_metrics_latency_is_seconds_without_a_unit_suffix(learned):
    ws, _ = learned
    f = ws.catalog_facts("play", "traces_spanmetrics_latency")
    assert (f.unit, f.type) == ("s", "histogram")


async def test_classic_histogram_family_from_bucket_sum_count(learned):
    ws, _ = learned
    e = entry(ws, "http_server_request_duration_seconds_bucket")
    assert e.fields["histogram_family"].value == [
        "http_server_request_duration_seconds_bucket",
        "http_server_request_duration_seconds_sum",
        "http_server_request_duration_seconds_count",
    ]
    assert e.fields["unit"].value == "s"


async def test_info_metrics_do_not_contradict_their_declared_gauge_type(learned):
    ws, _ = learned
    e = entry(ws, "kube_pod_info")
    assert e.fields["type"].value == "gauge" and "info" not in {c.value for c in e.claims["type"]}
    assert e.fields["additivity_series"].value == "none"


async def test_time_unit_suffixes_beat_the_count_fallback(learned):
    ws, _ = learned
    assert entry(ws, "runtime_uptime_milliseconds_total").fields["unit"].value == "ms"
    ns = entry(ws, "go_sql_connections_wait_duration_nanoseconds_total")
    assert ns.fields["unit"].value == "ns"
    assert not ns.conflicts()  # declared ns and the name rule now agree


async def test_declared_gauge_named_total_is_a_recorded_contradiction(learned):
    ws, _ = learned
    e = entry(ws, "loki_source_file_read_bytes_total")
    assert e.fields["type"].value == "gauge" and e.fields["type"].origin == "metadata"
    assert [(c.origin, c.value) for c in e.conflicts()["type"]] == [("rule", "counter")]
    # charts follow the declaration: no per-second transform for a declared gauge
    assert ws.catalog_facts("play", "loki_source_file_read_bytes_total").type == "gauge"
