"""Adapter + discovery against fixtures recorded from public sources, one family each.

Recorded by scripts/record_public_fixtures.py (trimmed); replayed with no network.
"""

import json
from pathlib import Path

import httpx
import pytest

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.public import PUBLIC_SOURCES
from telemetry_nerd.sources.replay import ReplayTransport
from telemetry_nerd.sources.spec import Politeness

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# (source, backend family the fixtures stand for)
RECORDED = [
    ("promlabs-demo", "prometheus"),
    ("prometheus-demo", "prometheus"),
    ("cern-eos", "thanos"),
    ("cern-openstack", "mimir"),
    ("vm-playground", "victoriametrics"),
]
HISTOGRAMS = {
    "promlabs-demo": "demo_api_request_duration_seconds_bucket",
    "prometheus-demo": "alertmanager_http_request_duration_seconds_bucket",
    "cern-eos": "go_gc_pauses_seconds_bucket",
    "vm-playground": "alertmanager_http_request_duration_seconds_bucket",
}
ids = [name for name, _ in RECORDED]


def window(name: str) -> tuple[TimeRange, int]:
    w = json.loads((FIXTURES / PUBLIC_SOURCES[name].fixtures / "window.json").read_text())
    return TimeRange(w["start_ms"], w["end_ms"]), w["step_ms"]


@pytest.fixture
def make_source():
    clients = []

    def make(name: str) -> PromQLSource:
        entry = PUBLIC_SOURCES[name]
        spec = entry.to_spec().model_copy(update={"politeness": Politeness()})  # no spacing offline
        client = httpx.AsyncClient(transport=ReplayTransport(FIXTURES / entry.fixtures))
        clients.append(client)
        return PromQLSource.from_spec(spec, client=client)

    yield make


def test_fixtures_cover_all_four_backend_families():
    assert {family for _, family in RECORDED} == {
        "prometheus",
        "thanos",
        "mimir",
        "victoriametrics",
    }
    for name, family in RECORDED:
        assert PUBLIC_SOURCES[name].backend == family


@pytest.mark.parametrize(("name", "family"), RECORDED, ids=ids)
def test_fixtures_are_small(name, family):
    files = list((FIXTURES / PUBLIC_SOURCES[name].fixtures).glob("*.json"))
    assert files
    assert sum(f.stat().st_size for f in files) < 200_000


@pytest.mark.parametrize(("name", "family"), RECORDED, ids=ids)
async def test_probe_replays(make_source, name, family):
    status = await make_source(name).probe()
    assert status["reachable"] is True
    assert status.get("version")


@pytest.mark.parametrize(("name", "family"), RECORDED, ids=ids)
async def test_discovery_replays(make_source, name, family):
    d = await make_source(name).discover()
    names = {m.name for m in d.metrics}
    assert "up" in names and len(names) > 100
    assert d.label_names and "__name__" in d.label_names
    assert 0.3 < d.metadata_coverage <= 1.0  # partial metadata is normal, not an error
    assert any(m.type == "gauge" for m in d.metrics)
    # families the backends differ on: cardinality comes from /status/tsdb, not Thanos/Mimir
    if family in ("thanos", "mimir"):
        assert d.cardinality is None and "cardinality_unavailable" in d.caveats
    else:
        assert d.cardinality
    if name in HISTOGRAMS:
        assert HISTOGRAMS[name].removesuffix("_bucket") in d.histograms


@pytest.mark.parametrize(("name", "family"), RECORDED, ids=ids)
async def test_fetch_values_replays(make_source, name, family):
    rng, step = window(name)
    res = await make_source(name).fetch_values("avg by (job) (node_load1)", rng, step)
    assert res.series.num_rows >= 1
    assert res.buckets.num_rows > 0
    assert res.partial == 0


@pytest.mark.parametrize("name", sorted(HISTOGRAMS))
async def test_fetch_histogram_replays(make_source, name):
    rng, step = window(name)
    dist = await make_source(name).fetch_histogram(HISTOGRAMS[name], ["job"], rng, step)
    assert dist.scheme.kind == "classic"
    assert dist.series.num_rows >= 1


@pytest.mark.parametrize(("name", "family"), RECORDED, ids=ids)
async def test_unrecorded_request_cannot_reach_the_network(make_source, name, family):
    from telemetry_nerd.sources.base import SourceUnavailable

    rng, step = window(name)
    with pytest.raises(SourceUnavailable):
        await make_source(name).fetch_values("never_recorded_metric_xyz", rng, step)


@pytest.mark.parametrize(("name", "family"), RECORDED, ids=ids)
async def test_source_connect_by_name_against_fixtures(tmp_path, name, family):
    from tests.unit.fakes import make_service

    def factory(spec):
        client = httpx.AsyncClient(
            transport=ReplayTransport(FIXTURES / PUBLIC_SOURCES[name].fixtures)
        )
        return PromQLSource.from_spec(
            spec.model_copy(update={"politeness": Politeness()}), client=client
        )

    svc = make_service(tmp_path, factory=factory)
    out = await svc.source_connect(PUBLIC_SOURCES[name].to_spec())
    assert out["status"]["reachable"] is True
    learned = await svc.learn(name)
    assert learned["metrics"] > 100
