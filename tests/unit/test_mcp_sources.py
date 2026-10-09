import functools
import json

import httpx
from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.core.bootstrap import source_factory
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.promql import PromQLSource
from tests.unit.fakes import make_service

URL = "https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom"


async def call(mcp, name, args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args)


def text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_connect_list_query_disconnect(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    r = await call(
        mcp,
        "source_connect",
        {
            "name": "play",
            "url": URL,
            "resolution": "20s",
            "min_interval": "500ms",
            "timeout": "60s",
        },
    )
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["source"]["resolution_ms"] == 20_000
    assert out["source"]["politeness"] == {
        "max_concurrency": 4,
        "min_interval_ms": 500,
        "timeout_s": 60.0,
    }
    assert out["status"]["reachable"] is True

    names = [s["name"] for s in json.loads(text(await call(mcp, "source_list", {})))["sources"]]
    assert names == ["default", "play"]

    q = await call(
        mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h", "source": "play"}
    )
    assert not q.is_error, text(q)

    d = await call(mcp, "source_disconnect", {"name": "play"})
    assert json.loads(text(d)) == {"disconnected": "play"}


async def test_token_like_auth_env_is_rejected_with_guidance(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "source_connect", {"name": "g", "url": URL, "auth_env": "glsa_abcdef0123"})
    assert r.is_error
    assert "NAME of an environment variable" in text(r)


async def test_url_with_credentials_rejected(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "source_connect", {"name": "g", "url": "https://u:p@grafana.test"})
    assert r.is_error
    assert "credentials in the url" in text(r)


async def test_missing_secret_is_a_tool_error_with_hint(tmp_path):
    mcp = build_mcp(make_service(tmp_path, factory=PromQLSource.from_spec), "http://x")
    r = await call(
        mcp, "source_connect", {"name": "g", "url": URL, "auth_env": "TN_SURELY_UNSET_VAR"}
    )
    assert r.is_error
    assert "TN_SURELY_UNSET_VAR" in text(r) and "hint:" in text(r)


async def test_oauth_and_auth_env_together_is_a_tool_error(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(
        mcp,
        "source_connect",
        {
            "name": "sso",
            "url": URL,
            "auth_env": "TN_SURELY_UNSET_VAR",
            "oauth_authorize_url": "https://idp.example.com/authorize",
            "oauth_token_url": "https://idp.example.com/token",
            "oauth_client_id": "tn-client",
        },
    )
    assert r.is_error
    assert "oauth_*" in text(r) or "not both" in text(r)


async def test_grafana_with_oauth_logs_in_then_probes_with_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("IDP_SECRET", "s3cret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})
        if request.url.path.endswith("/api/v1/status/buildinfo"):
            assert request.headers.get("Authorization") == "Bearer AT1"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "version": "2.1.0",
                        "revision": "abc123",
                        "branch": "HEAD",
                        "buildUser": "root@buildhost",
                        "goVersion": "go1.21",
                    }
                },
            )
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telemetry_nerd.sources.grafana.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.oauth.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )

    mcp = build_mcp(
        make_service(tmp_path, factory=lambda spec: source_factory(spec, tmp_path)), "http://x"
    )
    r = await call(
        mcp,
        "source_connect",
        {
            "name": "sso",
            "grafana": "https://play.grafana.org",
            "uid": "grafanacloud-prom",
            "oauth_authorize_url": "https://idp.example.com/authorize",
            "oauth_token_url": "https://idp.example.com/token",
            "oauth_client_id": "tn-client",
            "oauth_client_secret_env": "IDP_SECRET",
            "oauth_flow": "client_credentials",
        },
    )
    assert not r.is_error
    body = json.loads(text(r))
    assert body["backend"] == "prometheus"


async def test_partial_oauth_args_without_authorize_url_is_a_tool_error(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(
        mcp,
        "source_connect",
        {"name": "sso", "url": URL, "oauth_token_url": "https://idp.example.com/token"},
    )
    assert r.is_error
    assert "oauth_token_url" in text(r) and "oauth_authorize_url" in text(r)


async def test_status_unknown_source_is_tool_error(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "source_status", {"name": "ghost"})
    assert r.is_error and "source_list" in text(r)


async def test_unknown_argument_is_rejected_not_ignored(tmp_path):
    """source_connect(resolution_ms=...) used to be dropped silently (the default 15s applied):
    every tool now rejects arguments it does not take, and its schema says so."""
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "source_connect", {"name": "vm", "url": URL, "resolution_ms": 5000})
    assert r.is_error
    assert "resolution_ms" in text(r)
    names = [s["name"] for s in json.loads(text(await call(mcp, "source_list", {})))["sources"]]
    assert "vm" not in names
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
    assert all(t.input_schema.get("additionalProperties") is False for t in tools)


async def test_connect_an_elasticsearch_source_with_an_api_key(tmp_path, monkeypatch):
    async def ok(self) -> dict:
        return {"reachable": True, "distribution": "elasticsearch", "version": "8.15.3"}

    monkeypatch.setattr(ElasticsearchSource, "probe", ok)
    monkeypatch.setenv("ES_API_KEY", "abc==")
    svc = make_service(tmp_path, factory=functools.partial(source_factory, data_dir=tmp_path))
    r = await call(build_mcp(svc, "http://x"), "source_connect", {
        "name": "logs", "url": "https://es.example:9200", "flavor": "elasticsearch",
        "index_pattern": "access-logs-*", "time_field": "@timestamp",
        "auth_env": "ES_API_KEY", "auth_scheme": "apikey",
    })  # fmt: skip
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert (out["source"]["flavor"], out["source"]["index_pattern"]) == (
        "elasticsearch",
        "access-logs-*",
    )
    assert out["source"]["auth"]["scheme"] == "apikey"
    assert isinstance(svc.sources["logs"], ElasticsearchSource)


async def test_an_es_source_without_time_field_is_refused_with_the_reason(tmp_path):
    svc = make_service(tmp_path, factory=functools.partial(source_factory, data_dir=tmp_path))
    r = await call(build_mcp(svc, "http://x"), "source_connect", {
        "name": "logs", "url": "https://es.example:9200", "flavor": "elasticsearch",
        "index_pattern": "access-logs-*",
    })  # fmt: skip
    assert r.is_error and "time_field" in text(r)


async def test_client_credentials_oauth_source_connect_skips_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("IDP_SECRET", "s3cret")
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return httpx.Response(200, json={"access_token": "AT1", "expires_in": 3600})
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.oauth.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler)),
    )

    mcp = build_mcp(
        make_service(tmp_path, factory=lambda spec: source_factory(spec, tmp_path)), "http://x"
    )
    r = await call(
        mcp,
        "source_connect",
        {
            "name": "m2m",
            "url": URL,
            "oauth_authorize_url": "https://idp.example.com/authorize",
            "oauth_token_url": "https://idp.example.com/token",
            "oauth_client_id": "tn-client",
            "oauth_client_secret_env": "IDP_SECRET",
            "oauth_flow": "client_credentials",
        },
    )
    assert not r.is_error
    assert opened == []


async def test_source_connect_documents_the_es_flavors(tmp_path):
    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    doc = tools["source_connect"].description or ""
    for word in ("elasticsearch", "opensearch", "index_pattern", "time_field", "apikey"):
        assert word in doc, word
