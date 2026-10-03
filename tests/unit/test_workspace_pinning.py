"""One active-workspace scope per daemon, pinned per MCP tool call and per HTTP request (D4)."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import FakeSource, make_service

HOSTS = ["testserver"]


def _workspaces(svc, sql: str) -> list:
    return svc.workspace.connection.execute(sql).fetchall()


def test_make_service_shares_one_scope(tmp_path):
    svc = make_service(tmp_path)
    svc.registry.create("second", None)
    svc.active.set_active("w2")
    assert svc.ws.workspace.list_panels() == []  # reads follow the active id
    assert svc.ws.objects.list_annotations() == []
    assert svc.workspace.get_setting("default_range", "now-1h") == "now-1h"


def test_default_range_is_per_workspace(tmp_path):
    svc = make_service(tmp_path)
    svc.set_default_range("now-6h")
    svc.registry.create("second", None)
    svc.active.set_active("w2")
    assert svc.get_default_range() == "now-1h"
    svc.active.set_active("w1")
    assert svc.get_default_range() == "now-6h"


def _switch_back_mid_request(svc, monkeypatch) -> None:
    """The user switches to w1 while the request is between entry and its write: the write
    must still land in the workspace pinned at entry."""
    create = svc.ws.objects.create_annotation

    def switching(*args, **kwargs):
        svc.active.set_active("w1")
        return create(*args, **kwargs)

    monkeypatch.setattr(svc.ws.objects, "create_annotation", switching)


async def test_sync_mcp_tool_sees_the_pin(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://ui")
    svc.registry.create("second", None)
    svc.active.set_active("w2")
    _switch_back_mid_request(svc, monkeypatch)
    await mcp.call_tool("annotate", {"kind": "threshold", "value": 1.0, "label": "here"})
    assert _workspaces(svc, "SELECT workspace FROM objects") == [("w2",)]


class GatedSource(FakeSource):
    """Holds every fetch until `gate` is set, so a switch can land mid-flight."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def fetch(self, expr, rng, step_ms):
        self.started.set()
        await self.gate.wait()
        return await super().fetch(expr, rng, step_ms)


async def test_in_flight_tool_finishes_in_its_workspace(tmp_path):
    source = GatedSource()
    svc = make_service(tmp_path, source=source)
    mcp = build_mcp(svc, "http://ui")
    svc.registry.create("second", None)
    task = asyncio.create_task(mcp.call_tool("query", {"expr": "up"}))
    await source.started.wait()
    svc.active.set_active("w2")
    source.gate.set()
    await task
    rows = _workspaces(svc, "SELECT workspace FROM events WHERE type = 'dataset.created'")
    assert rows == [("w1",)]


def test_http_route_is_pinned(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    with TestClient(create_app(svc, allowed_hosts=HOSTS)) as c:
        svc.registry.create("second", None)
        svc.active.set_active("w2")
        _switch_back_mid_request(svc, monkeypatch)
        r = c.post("/api/annotations", json={"kind": "threshold", "value": 1.0, "label": "x"})
        assert r.status_code < 300, r.text
    assert _workspaces(svc, "SELECT workspace FROM objects") == [("w2",)]


async def test_claude_reply_lands_in_the_threads_workspace(tmp_path):
    # channel delivery is global: a question asked in w1 may be answered after a switch to w2
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://ui")
    thread = svc.ws.ask("why the spike?", "user")
    svc.registry.create("second", None)
    svc.active.set_active("w2")
    result = await mcp.call_tool("reply", {"thread": thread.id, "text": "a deploy"})
    message = json.loads(_text(result))["message"]
    assert _workspaces(svc, f"SELECT workspace FROM objects WHERE id = '{message}'") == [("w1",)]
    events = _workspaces(
        svc, "SELECT workspace FROM events WHERE type = 'thread.message' AND actor = 'claude'"
    )
    assert events == [("w1",)]


async def test_user_message_to_another_workspaces_thread_is_refused(tmp_path):
    svc = make_service(tmp_path)
    thread = svc.ws.ask("why the spike?", "user")
    svc.registry.create("second", None)
    svc.active.set_active("w2")
    with svc.active.pinned(), pytest.raises(ValueError, match="w1"):
        svc.ws.post_message(thread.id, "more", "user")


async def test_reply_to_a_missing_thread_still_fails(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://ui")
    with pytest.raises(ToolError):
        await mcp.call_tool("reply", {"thread": "t99", "text": "x"})


def _text(result) -> str:
    return result.content[0].text


async def test_claude_tool_call_over_streamable_http_follows_its_own_switch(tmp_path):
    """Regression: the /mcp session opened in w1 must not stay pinned to w1 after
    workspace_create; the next tool call writes into the new workspace."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    svc = make_service(tmp_path)
    url = "http://testserver"
    app = create_app(svc, allowed_hosts=HOSTS, mcp=build_mcp(svc, url))
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=url)
    transport = streamable_http_client(f"{url}/mcp", http_client=http)
    async with app.router.lifespan_context(app), http, Client(transport) as client:
        created = await client.call_tool("workspace_create", {"title": "second"})
        assert not created.is_error
        assert svc.active.active == "w2"
        made = await client.call_tool("hypothesis_create", {"statement": "a deploy did it"})
        assert not made.is_error
    rows = _workspaces(svc, "SELECT workspace FROM objects WHERE kind = 'hypothesis'")
    assert rows == [("w2",)]


def test_current_workspace_reads_the_pinned_one(tmp_path):
    svc = make_service(tmp_path)
    svc.registry.create("second", None)
    with svc.active.using("w1"):
        svc.active.set_active("w2")  # a switch lands mid-call
        assert svc.ws.current()["id"] == "w1"


async def test_workspace_update_fans_its_frame_out_on_the_event_loop(tmp_path, monkeypatch):
    """asyncio.Queue is not thread-safe: the tool must notify from the loop thread."""
    import threading

    from mcp import Client

    svc = make_service(tmp_path)
    threads: list[int] = []
    notify = svc.active.notify
    monkeypatch.setattr(
        svc.active, "notify", lambda f: (threads.append(threading.get_ident()), notify(f))
    )
    async with Client(build_mcp(svc, "http://x")) as client:
        r = await client.call_tool("workspace_update", {"id": "w1", "title": "renamed"})
    assert not r.is_error
    assert threads == [threading.get_ident()]
