import asyncio
import json
import time

import httpx
import pytest

from telemetry_nerd.core.bootstrap import source_factory
from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.oauth import TokenState, save_token, token_path
from telemetry_nerd.sources.spec import AuthRef, SourceSpec
from tests.unit.fakes import FakeSource, make_service

URL = "https://play.test/prom"


class DownSource(FakeSource):
    async def probe(self) -> dict:
        raise SourceUnavailable("cannot reach", hint="check the URL")


async def test_connect_probes_persists_and_logs(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.source_connect(SourceSpec(name="play", url=URL))
    assert out["source"]["name"] == "play"
    assert out["status"]["reachable"] is True
    assert "play" in svc.sources
    [event] = [e for e in svc.log.since(0, 100) if e.type == "source.connected"]
    assert event.object_id == "play" and event.actor == "claude"


async def test_query_routes_to_named_source(tmp_path):
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="play", url=URL))
    await svc.query("up", "now-2h", "now-1h", source="play")
    assert svc.sources["play"].calls > 0  # the cache may fetch in several chunks
    assert svc.sources["default"].calls == 0


async def test_unreachable_source_is_not_added(tmp_path):
    svc = make_service(tmp_path, factory=lambda s: DownSource(name=s.name))
    with pytest.raises(SourceUnavailable):
        await svc.source_connect(SourceSpec(name="down", url=URL))
    assert "down" not in svc.sources
    assert all(d["name"] != "down" for d in svc.source_list())


async def test_reserved_name_refused(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError, match="reserved"):
        await svc.source_connect(SourceSpec(name="default", url=URL))
    with pytest.raises(SourceError, match="reserved"):
        await svc.source_disconnect("default")


async def test_duplicate_refused_before_probing(tmp_path):
    probes = []

    class Counting(FakeSource):
        async def probe(self) -> dict:
            probes.append(self.name)
            return {"reachable": True}

    svc = make_service(tmp_path, factory=lambda s: Counting(name=s.name))
    await svc.source_connect(SourceSpec(name="play", url=URL))
    with pytest.raises(SourceError, match="already exists"):
        await svc.source_connect(SourceSpec(name="play", url=URL))
    assert probes == ["play"]
    await svc.source_connect(SourceSpec(name="play", url=URL + "/v2"), replace=True)
    assert probes == ["play", "play"]


async def test_disconnect_removes_and_logs(tmp_path):
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="play", url=URL))
    await svc.source_disconnect("play")
    assert "play" not in svc.sources
    assert any(e.type == "source.disconnected" for e in svc.log.since(0, 100))


async def test_status_reports_unreachable_without_raising(tmp_path):
    svc = make_service(tmp_path)
    svc.sources.attach("down", DownSource(name="down"))
    out = await svc.source_status("down")
    assert out["reachable"] is False
    assert out["hint"] == "check the URL"
    with pytest.raises(SourceError, match="unknown source"):
        await svc.source_status("ghost")


async def test_secret_never_in_list_or_events(tmp_path, monkeypatch):
    monkeypatch.setenv("TN_TEST_TOKEN", "s3cr3t-value")
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="auth", url=URL, auth=AuthRef(env="TN_TEST_TOKEN")))
    blob = json.dumps(svc.source_list()) + json.dumps([e.to_dict() for e in svc.log.since(0, 100)])
    assert "s3cr3t-value" not in blob
    assert "TN_TEST_TOKEN" in blob


def _oauth_spec(**kw) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "name": "sso",
            "url": "http://prom.example.com",
            "auth": {
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "client_id": "tn-client",
            },
            **kw,
        }
    )


def _real_factory(tmp_path):
    return lambda spec: source_factory(spec, tmp_path)


async def test_source_connect_with_oauth_and_a_valid_token_connects_without_logging_in(
    tmp_path, monkeypatch
):
    save_token(token_path(tmp_path, "sso"), TokenState("AT0", "RT0", time.time() + 3600))
    service = make_service(tmp_path, factory=_real_factory(tmp_path))

    def fake_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    # patching `promql.httpx.AsyncClient` mutates the shared httpx module, so the
    # replacement must close over the real class captured before the patch, or every
    # subsequent `httpx.AsyncClient(...)` call (including this lambda's own) recurses
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(fake_transport)),
    )
    out = await service.source_connect(_oauth_spec())
    assert service.sources.spec("sso") is not None
    assert out["source"]["auth"]["client_id"] == "tn-client"


async def test_source_connect_with_oauth_and_no_token_drives_a_real_login(tmp_path, monkeypatch):
    service = make_service(tmp_path, factory=_real_factory(tmp_path))

    def fake_transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.example.com":
            return httpx.Response(
                200, json={"access_token": "AT0", "refresh_token": "RT0", "expires_in": 3600}
            )
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "telemetry_nerd.sources.promql.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(fake_transport)),
    )
    monkeypatch.setattr(
        "telemetry_nerd.sources.oauth.httpx.AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(fake_transport)),
    )

    connect_task = asyncio.create_task(service.source_connect(_oauth_spec()))

    async def fire_once_listening() -> None:
        login_url = None
        for _ in range(200):  # ~2s worst case
            events = [e for e in service.log.tail(50) if e.type == "source.oauth_login_url"]
            if events:
                login_url = events[-1].payload["url"]
                break
            await asyncio.sleep(0.01)
        assert login_url is not None
        params = dict(httpx.QueryParams(httpx.URL(login_url).params))
        state = params["state"]
        redirect_uri = params["redirect_uri"]
        async with real_async_client() as c:  # a real connection to the local listener
            await c.get(redirect_uri, params={"code": "code123", "state": state})

    await asyncio.gather(connect_task, fire_once_listening())
    assert service.sources.spec("sso") is not None
