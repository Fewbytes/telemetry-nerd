"""Kibana discovery front door: discover_index_patterns (mocked httpx, no network) and the
source_discover_kibana MCP tool, mirroring tests/unit/test_grafana.py."""

import json

import httpx
import pytest
from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.kibana import discover_index_patterns
from telemetry_nerd.sources.spec import AuthRef
from tests.unit.fakes import make_service


def text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _find_response(objects: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, json={"page": 1, "per_page": 1000, "total": len(objects), "saved_objects": objects}
    )


# -- discover_index_patterns: success, auth, errors --------------------------------------


async def test_discover_lists_patterns_with_time_fields():
    objects = [
        {
            "type": "index-pattern",
            "id": "logs-id",
            "attributes": {"title": "logs-*", "timeFieldName": "@timestamp"},
        },
        {
            "type": "index-pattern",
            "id": "metrics-id",
            "attributes": {"title": "metrics-*", "timeFieldName": "metadata.timestamp"},
        },
    ]
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = str(request.url.params)
        captured["xsrf"] = request.headers.get("kbn-xsrf")
        return _find_response(objects)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    patterns = await discover_index_patterns("https://kibana.example", client=client)

    assert captured["path"] == "/api/saved_objects/_find"
    assert captured["xsrf"] == "true"
    by_title = {p.title: p for p in patterns}
    assert by_title["logs-*"].id == "logs-id"
    assert by_title["logs-*"].time_field == "@timestamp"
    assert by_title["metrics-*"].time_field == "metadata.timestamp"
    assert by_title["logs-*"].describe() == {
        "id": "logs-id",
        "title": "logs-*",
        "time_field": "@timestamp",
    }


async def test_discover_pattern_without_time_field_omits_it():
    objects = [{"type": "index-pattern", "id": "x", "attributes": {"title": "no-tf-*"}}]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _find_response(objects)))
    patterns = await discover_index_patterns("https://kibana.example", client=client)
    assert patterns[0].time_field is None
    assert patterns[0].describe() == {"id": "x", "title": "no-tf-*"}


async def test_discover_empty_result():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _find_response([])))
    patterns = await discover_index_patterns("https://kibana.example", client=client)
    assert patterns == []


async def test_discover_sends_auth_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return _find_response([])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = AuthRef(env="KIBANA_TOKEN", scheme="bearer")
    await discover_index_patterns(
        "https://kibana.example", auth, client=client, environ={"KIBANA_TOKEN": "tok"}
    )
    assert captured["auth"] == "Bearer tok"


async def test_discover_unauthorized_raises_source_error_with_hint():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(SourceError) as exc_info:
        await discover_index_patterns("https://kibana.example", client=client)
    assert exc_info.value.hint is not None and "auth_env" in exc_info.value.hint


async def test_discover_non_json_raises_source_unavailable():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>not json</html>"))
    )
    with pytest.raises(SourceError):
        await discover_index_patterns("https://kibana.example", client=client)


async def test_discover_bad_shape_raises_source_error():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"not": "expected"}))
    )
    with pytest.raises(SourceError):
        await discover_index_patterns("https://kibana.example", client=client)


async def test_discover_unreachable_host_raises_source_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError):
        await discover_index_patterns("https://nope.example", client=client)


# -- MCP tool: source_discover_kibana ------------------------------------------------------


async def test_source_discover_kibana_tool(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    objects = [
        {
            "type": "index-pattern",
            "id": "logs-id",
            "attributes": {"title": "logs-*", "timeFieldName": "@timestamp"},
        }
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _find_response(objects)))

    async def fake_discover(url, auth=None, **kw):
        return await discover_index_patterns(url, auth, client=client)

    monkeypatch.setattr("telemetry_nerd.mcp.server.discover_index_patterns", fake_discover)
    async with Client(mcp) as mcp_client:
        r = await mcp_client.call_tool("source_discover_kibana", {"url": "https://kibana.example"})
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["kibana_url"] == "https://kibana.example"
    assert out["index_patterns"] == [
        {"id": "logs-id", "title": "logs-*", "time_field": "@timestamp"}
    ]


async def test_source_discover_kibana_tool_surfaces_source_error(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")

    async def fake_discover(url, auth=None, **kw):
        raise SourceError("boom", hint="check the url")

    monkeypatch.setattr("telemetry_nerd.mcp.server.discover_index_patterns", fake_discover)
    async with Client(mcp) as mcp_client:
        r = await mcp_client.call_tool("source_discover_kibana", {"url": "https://kibana.example"})
    assert r.is_error
    assert "boom" in text(r)
