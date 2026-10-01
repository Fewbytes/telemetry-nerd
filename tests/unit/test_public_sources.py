"""The public source registry: shape, politeness, and connect-by-name (no network)."""

import json

import httpx
import pytest
from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.sources.presets import PRESETS
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.public import PUBLIC_SOURCES, PublicSource, load_public_sources
from tests.unit.fakes import make_service

EXPECTED = {
    "grafana-play",
    "wikimedia-thanos",
    "cern-eos",
    "cern-openstack",
    "cern-dbod",
    "percona-pmm",
    "vm-playground",
    "prometheus-demo",
    "promlabs-demo",
}


def text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_registry_lists_every_spike_source():
    assert set(PUBLIC_SOURCES) == EXPECTED
    assert load_public_sources() == PUBLIC_SOURCES


def test_all_four_backend_families_are_covered():
    assert {s.backend for s in PUBLIC_SOURCES.values()} == {
        "prometheus",
        "thanos",
        "mimir",
        "victoriametrics",
    }


@pytest.mark.parametrize("entry", PUBLIC_SOURCES.values(), ids=lambda e: e.name)
def test_entry_documents_politeness_and_use(entry: PublicSource):
    assert entry.suggested_use.strip()
    assert entry.max_range_s > 0  # documented ceiling for one query's time range
    pol = entry.politeness
    assert pol.max_concurrency <= 2
    assert pol.min_interval_ms >= 500
    assert pol.timeout_s >= 30


@pytest.mark.parametrize("entry", PUBLIC_SOURCES.values(), ids=lambda e: e.name)
def test_entry_builds_a_valid_spec(entry: PublicSource):
    spec = entry.to_spec()
    assert spec.name == entry.name
    assert spec.url == entry.url
    assert spec.politeness == entry.politeness
    assert (spec.flavor == "victoriametrics") == (entry.backend == "victoriametrics")


def test_legacy_presets_agree_with_the_registry():
    assert PRESETS["play"].url == PUBLIC_SOURCES["grafana-play"].url
    assert PRESETS["wikimedia"].url == PUBLIC_SOURCES["wikimedia-thanos"].url


def test_nothing_is_connected_by_default(tmp_path):
    svc = build_service(Settings(data_dir=tmp_path, source_url="http://127.0.0.1:9"))
    assert [s["name"] for s in svc.source_list()] == ["default"]


def _offline_factory(spec):
    """A real PromQLSource over a mock server that answers buildinfo (no network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host in spec.url, "request left the registry URL"
        return httpx.Response(200, json={"status": "success", "data": {"version": "0.0-test"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PromQLSource.from_spec(spec, client=client)


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_source_connect_by_name(tmp_path, name):
    svc = make_service(tmp_path, factory=_offline_factory)
    mcp = build_mcp(svc, "http://x")
    async with Client(mcp) as client:
        r = await client.call_tool("source_connect", {"name": name})
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    entry = PUBLIC_SOURCES[name]
    assert out["source"]["url"] == entry.url
    assert out["source"]["flavor"] == entry.to_spec().flavor
    assert out["source"]["politeness"]["max_concurrency"] == entry.politeness.max_concurrency
    assert out["status"]["reachable"] is True
    assert name in svc.sources


async def test_unknown_name_without_url_lists_the_registry(tmp_path):
    mcp = build_mcp(make_service(tmp_path, factory=_offline_factory), "http://x")
    async with Client(mcp) as client:
        r = await client.call_tool("source_connect", {"name": "nope"})
    assert r.is_error
    assert "grafana-play" in text(r)


async def test_explicit_url_still_works_and_public_sources_tool_lists_registry(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    async with Client(mcp) as client:
        r = await client.call_tool("source_connect", {"name": "mine", "url": "http://p:9090"})
        assert not r.is_error, text(r)
        listing = json.loads(text(await client.call_tool("public_sources", {})))
    assert {s["name"] for s in listing["sources"]} == EXPECTED
    one = next(s for s in listing["sources"] if s["name"] == "grafana-play")
    assert one["backend"] == "mimir" and one["max_range"] and one["suggested_use"]
