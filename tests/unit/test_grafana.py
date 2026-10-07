"""Grafana front door: discover datasources from a Grafana URL and connect one through
its proxy, with the right backend/flavor detected from buildinfo.

Discovery fixtures recorded by scripts/record_grafana_fixtures.py (trimmed), replayed
with no network. detect_backend is pure and tested directly against the shapes
VERIFIED on the public-sources buildinfo fixtures (tests/fixtures/missing-data/*).
"""

import json

import httpx
import pytest
from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.grafana import detect_backend, discover_datasources, probe_backend
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import ReplayTransport
from telemetry_nerd.sources.spec import SourceSpec
from tests.unit.fakes import make_service

FIXTURES = __import__("pathlib").Path(__file__).resolve().parent.parent / "fixtures" / "grafana"


def text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# -- detect_backend: pure, against the shapes VERIFIED on public-sources fixtures --------


def test_detects_mimir_from_application_field():
    # grafana-play and cern-openstack buildinfo (tests/fixtures/missing-data/mimir/*)
    data = {"application": "Grafana Mimir", "version": "r411-926f316c", "goVersion": "go1.26.7"}
    assert detect_backend(data) == ("mimir", "prometheus")


def test_detects_victoriametrics_from_minimal_shape():
    # vm-playground and percona-pmm buildinfo (tests/fixtures/missing-data/victoriametrics/*):
    # no application field, and none of Prometheus/Thanos's revision/branch/goVersion
    data = {"version": "2.24.0"}
    assert detect_backend(data) == ("victoriametrics", "victoriametrics")


def test_detects_victoriametrics_from_application_field_too():
    assert detect_backend({"application": "VictoriaMetrics Cluster"}) == (
        "victoriametrics",
        "victoriametrics",
    )


def test_prometheus_and_thanos_are_indistinguishable_and_default_to_prometheus_flavor():
    # prometheus-demo and wikimedia-thanos buildinfo (tests/fixtures/missing-data/*): same
    # shape, no application field; flavor is what matters for querying and is the same
    data = {
        "version": "3.13.0",
        "revision": "40af9c2c",
        "branch": "HEAD",
        "buildUser": "root@x",
        "buildDate": "20260701",
        "goVersion": "go1.26.4",
    }
    assert detect_backend(data) == ("prometheus", "prometheus")


def test_unrecognised_application_keeps_prometheus_flavor():
    assert detect_backend({"application": "Cortex"}) == ("mimir", "prometheus")
    backend, flavor = detect_backend({"application": "SomeFutureEngine"})
    assert backend == "somefutureengine" and flavor == "prometheus"


# -- discovery against recorded fixtures (no network) ------------------------------------


@pytest.fixture
def replay_client():
    clients = []

    def make(name: str) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=ReplayTransport(FIXTURES / name))
        clients.append(client)
        return client

    yield make


async def test_discover_play_lists_mimir_and_marks_unsupported(replay_client):
    datasources = await discover_datasources(
        "https://play.grafana.org", client=replay_client("play")
    )
    by_name = {d.name: d for d in datasources}
    mimir = by_name["grafanacloud-play-prom"]
    assert mimir.uid == "grafanacloud-prom"
    assert mimir.type == "prometheus"
    assert mimir.backend_hint == "Mimir"
    assert mimir.supported is True
    assert mimir.proxy_url("https://play.grafana.org/") == (
        "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom"
    )

    plain_prom = by_name["grafanacloud-ml-metrics"]
    assert plain_prom.type == "prometheus" and plain_prom.backend_hint is None
    assert plain_prom.supported is True

    unsupported = by_name["AWS IoT SiteWise"]
    assert unsupported.supported is False
    desc = unsupported.describe()
    assert desc["supported"] is False and "not Prometheus" in desc["reason"]


async def test_discover_wikimedia_lists_thanos_hint(replay_client):
    datasources = await discover_datasources(
        "https://grafana.wikimedia.org", client=replay_client("wikimedia")
    )
    by_name = {d.name: d for d in datasources}
    thanos = by_name["thanos"]
    assert thanos.uid == "000000026"
    assert thanos.backend_hint == "Thanos"
    assert thanos.supported is True
    grafana_builtin = by_name["-- Grafana --"]
    assert grafana_builtin.supported is False  # type "datasource", not "prometheus"


async def test_discover_bad_host_raises_source_error():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(SourceError):
        await discover_datasources("https://nope.example", client=client)


# -- buildinfo-based flavor detection, through the proxy, against fixtures ---------------


async def test_probe_backend_detects_mimir_through_proxy(replay_client):
    backend, flavor = await probe_backend(
        "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom",
        client=replay_client("play"),
    )
    assert (backend, flavor) == ("mimir", "prometheus")


async def test_probe_backend_falls_back_when_buildinfo_unreachable():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    backend, flavor = await probe_backend("https://x/api/datasources/proxy/uid/u", client=client)
    assert (backend, flavor) == ("prometheus", "prometheus")


# -- MCP tools: source_discover_grafana, source_connect(grafana=, uid=) -------------------


async def test_source_discover_grafana_tool(tmp_path, replay_client, monkeypatch):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    client = replay_client("play")

    async def fake_discover(url, auth=None, **kw):
        return await discover_datasources(url, auth, client=client)

    monkeypatch.setattr("telemetry_nerd.mcp.server.discover_datasources", fake_discover)
    async with Client(mcp) as mcp_client:
        r = await mcp_client.call_tool(
            "source_discover_grafana", {"url": "https://play.grafana.org"}
        )
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["grafana_url"] == "https://play.grafana.org"
    names = {d["name"] for d in out["datasources"]}
    assert "grafanacloud-play-prom" in names and "AWS IoT SiteWise" in names


def _offline_mimir_factory(spec):
    """A real PromQLSource over a mock buildinfo+query server (no network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status/buildinfo"):
            return httpx.Response(
                200, json={"status": "success", "data": {"application": "Grafana Mimir"}}
            )
        return httpx.Response(200, json={"status": "success", "data": {"version": "x"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PromQLSource.from_spec(spec, client=client)


async def test_source_connect_grafana_uid_detects_flavor_and_builds_proxy_url(
    tmp_path, monkeypatch
):
    svc = make_service(tmp_path, factory=_offline_mimir_factory)
    mcp = build_mcp(svc, "http://x")

    async def fake_probe_backend(proxy_url, auth=None, **kw):
        assert proxy_url == "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom"
        return "mimir", "prometheus"

    monkeypatch.setattr("telemetry_nerd.mcp.server.probe_backend", fake_probe_backend)
    async with Client(mcp) as client:
        r = await client.call_tool(
            "source_connect",
            {
                "name": "play-mimir",
                "grafana": "https://play.grafana.org",
                "uid": "grafanacloud-prom",
            },
        )
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["backend"] == "mimir"
    assert (
        out["source"]["url"]
        == "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom"
    )
    assert out["source"]["flavor"] == "prometheus"
    assert out["status"]["reachable"] is True
    assert "play-mimir" in svc.sources


def _offline_vm_factory(spec):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {"version": "x"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PromQLSource.from_spec(spec, client=client)


async def test_source_connect_grafana_uid_detects_victoriametrics(tmp_path, monkeypatch):
    svc = make_service(tmp_path, factory=_offline_vm_factory)
    mcp = build_mcp(svc, "http://x")

    async def fake_probe_backend(proxy_url, auth=None, **kw):
        return "victoriametrics", "victoriametrics"

    monkeypatch.setattr("telemetry_nerd.mcp.server.probe_backend", fake_probe_backend)
    async with Client(mcp) as client:
        r = await client.call_tool(
            "source_connect",
            {"name": "vm1", "grafana": "https://g.example", "uid": "vmuid"},
        )
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["backend"] == "victoriametrics"
    assert out["source"]["flavor"] == "victoriametrics"


async def test_source_connect_grafana_without_uid_is_a_tool_error(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    async with Client(mcp) as client:
        r = await client.call_tool("source_connect", {"name": "x", "grafana": "https://g.example"})
    assert r.is_error
    assert "uid" in text(r)


async def test_source_connect_grafana_and_url_together_is_a_tool_error(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    async with Client(mcp) as client:
        r = await client.call_tool(
            "source_connect",
            {"name": "x", "grafana": "https://g.example", "uid": "u", "url": "http://p:9090"},
        )
    assert r.is_error
    assert "not both" in text(r)


def test_spec_rejects_nothing_new():
    # SourceSpec itself is unchanged by this feature: a proxy url is just a url
    spec = SourceSpec.model_validate(
        {"name": "x", "url": "https://g.example/api/datasources/proxy/uid/u"}
    )
    assert spec.url == "https://g.example/api/datasources/proxy/uid/u"


def test_an_elasticsearch_datasource_points_at_the_direct_connection():
    from telemetry_nerd.sources.grafana import UNSUPPORTED_HINT

    assert "not Prometheus" in UNSUPPORTED_HINT
    assert 'source_connect(url=..., flavor="elasticsearch"' in UNSUPPORTED_HINT
    assert "index_pattern" in UNSUPPORTED_HINT and "time_field" in UNSUPPORTED_HINT
