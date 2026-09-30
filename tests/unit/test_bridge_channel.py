"""Bridge: stdio MCP proxy + Claude Code channel delivery (Task 6)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import anyio
import httpx
import pytest
from mcp import Client
from mcp.server.context import ServerRequestContext
from mcp.types import ClientCapabilities, Notification, ServerNotification, TextContent

from telemetry_nerd.bridge.channel import ChannelGate, ChannelPump
from telemetry_nerd.bridge.proxy import (
    CHANNEL_NOTIFICATION,
    EXPERIMENTAL_CAPABILITIES,
    ChannelDelivery,
    build_bridge,
    client_advertised_channel,
)


async def _until(predicate: Callable[[], bool | Awaitable[bool]], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await anyio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


async def _get_json(url: str, **kw: Any) -> Any:
    async with httpx.AsyncClient() as client:
        return (await client.get(url, **kw)).json()


async def _post_json(url: str, **kw: Any) -> Any:
    async with httpx.AsyncClient() as client:
        return (await client.post(url, **kw)).json()


async def _channel_active(url: str, consumer: str = "claude") -> bool:
    return (await _get_json(f"{url}/api/channel/status", params={"consumer": consumer}))[
        "channel_active"
    ]


def _text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# --- gate and capability detection -------------------------------------------


def test_gate_reads_tn_channel_env():
    assert ChannelGate(env={"TN_CHANNEL": "1"}).enabled
    assert not ChannelGate(env={"TN_CHANNEL": "0"}).enabled
    assert not ChannelGate(env={}).enabled


async def test_gate_wait_unblocks_on_enable():
    gate = ChannelGate(env={})
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(gate.wait_enabled)
            await anyio.sleep(0.05)
            assert not gate.enabled
            gate.enable()
    assert gate.enabled


def test_client_advertised_channel():
    assert client_advertised_channel(ClientCapabilities(experimental={"claude/channel": {}}))
    assert not client_advertised_channel(None)
    assert not client_advertised_channel(ClientCapabilities())
    assert not client_advertised_channel(ClientCapabilities(experimental={}))
    assert not client_advertised_channel(ClientCapabilities(experimental={"other/x": {}}))


class _FakeSession:
    def __init__(self, caps: ClientCapabilities | None) -> None:
        self.client_capabilities = caps


class _FakeCtx:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session


def _ctx_with(caps: ClientCapabilities | None) -> ServerRequestContext[Any]:
    return cast("ServerRequestContext[Any]", _FakeCtx(_FakeSession(caps)))


def test_delivery_observes_client_capability_and_opens_gate():
    delivery = ChannelDelivery(ChannelGate(env={}))
    delivery.observe(_ctx_with(ClientCapabilities(experimental={"claude/channel": {}})))
    assert delivery.gate.enabled
    delivery = ChannelDelivery(ChannelGate(env={}))
    delivery.observe(_ctx_with(ClientCapabilities()))
    assert not delivery.gate.enabled


class _RecordingSession:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send_notification(
        self, notification: ServerNotification, related_request_id: Any = None
    ) -> None:
        self.sent.append(notification)


async def test_delivery_sends_channel_notification():
    delivery = ChannelDelivery(ChannelGate(env={}))
    assert delivery.session is None
    delivery.session = _RecordingSession()  # type: ignore[assignment]
    await delivery.notify('user asked in t1: "why?"', {"event": "thread.message", "seqs": "1"})
    session = delivery.session
    assert isinstance(session, _RecordingSession)
    assert len(session.sent) == 1
    wire = session.sent[0].model_dump(by_alias=True)
    assert wire["method"] == CHANNEL_NOTIFICATION
    assert wire["params"]["content"] == 'user asked in t1: "why?"'
    assert wire["params"]["meta"] == {"event": "thread.message", "seqs": "1"}


def test_run_bridge_init_options_declare_channel_only():
    bridge = build_bridge("http://127.0.0.1:7070")
    opts = bridge.create_initialization_options(experimental_capabilities=EXPERIMENTAL_CAPABILITIES)
    assert opts.capabilities.experimental == {"claude/channel": {}}
    assert opts.capabilities.tools is not None


# --- proxy against the live daemon -------------------------------------------


async def test_bridge_handshake_captures_session(live_daemon):
    delivery = ChannelDelivery(ChannelGate(env={}))
    bridge = build_bridge(live_daemon.url, delivery=delivery)
    assert not delivery.gate.enabled
    async with Client(bridge, mode="legacy") as client:
        assert client.session.server_capabilities is not None
        assert client.session.server_capabilities.tools is not None
        assert "Telemetry Nerd workspace" in (client.session.instructions or "")
        assert live_daemon.url in (client.session.instructions or "")
        # notifications/initialized is dispatched asynchronously by the runner;
        # give it a moment to land before asserting the capture.
        await asyncio.sleep(0.3)
        # notifications/initialized observed the handshake: session captured,
        # but the gate stays closed (this client advertised nothing).
        assert delivery.session is not None
        assert not delivery.gate.enabled


async def test_bridge_lists_daemon_tools_and_forwards_calls(live_daemon):
    bridge = build_bridge(live_daemon.url)
    async with Client(live_daemon.mcp_url) as daemon:
        daemon_tools = (await daemon.list_tools()).tools
        direct = await daemon.call_tool("workspace_get", {})
    assert [t.name for t in daemon_tools]
    async with Client(bridge) as client:
        tools = (await client.list_tools()).tools
        assert [(t.name, t.description, t.input_schema) for t in tools] == [
            (t.name, t.description, t.input_schema) for t in daemon_tools
        ]
        brief = await client.call_tool("workspace_get", {})
        bad = await client.call_tool("reply", {"thread": "t99", "text": "x"})
    assert not brief.is_error
    assert _text(brief) == _text(direct)
    assert bad.is_error
    assert "hint" in _text(bad)


# --- pump against the live daemon --------------------------------------------


def test_bridge_cli_resolves_url(tmp_path, monkeypatch):
    from telemetry_nerd import cli, daemon

    settings = cli.Settings.from_env()
    settings.data_dir = tmp_path
    log = logging.getLogger("test")

    spawned = []
    monkeypatch.setattr(
        daemon, "ensure_daemon", lambda s, wait_s=15.0: spawned.append(s) or "http://d"
    )
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: False)
    # Default: autostart (or adopt) the daemon.
    assert cli._bridge_url(cli._parse(["bridge"]), settings, log) == "http://d"
    assert spawned
    # --no-autostart: use the configured URL without spawning.
    spawned.clear()
    assert (
        cli._bridge_url(cli._parse(["bridge", "--no-autostart"]), settings, log)
        == settings.daemon_url
    )
    assert not spawned
    # Explicit --daemon-url: never spawns, and an unhealthy one exits 1.
    with pytest.raises(SystemExit) as e:
        cli._bridge_url(cli._parse(["bridge", "--daemon-url", "http://dead:1"]), settings, log)
    assert e.value.code == 1
    assert not spawned


async def test_pump_delivers_user_thread_once(live_daemon, monkeypatch):
    monkeypatch.setenv("TN_CHANNEL", "1")
    seen: list[tuple[str, dict[str, Any]]] = []
    got = anyio.Event()

    async def notify(content: str, meta: dict[str, Any]) -> None:
        seen.append((content, meta))
        got.set()

    pump = ChannelPump(live_daemon.url, notify)
    tid = ""
    async with anyio.create_task_group() as tg:
        tg.start_soon(pump.run)
        # Deterministic start: the pump's /ws subscription exists (so the thread
        # event cannot be missed) and its heartbeat made the channel active.
        await _until(lambda: live_daemon.service.log.subscriber_count > 0)
        await _until(lambda: _channel_active(live_daemon.url))
        thread = await _post_json(
            f"{live_daemon.url}/api/threads", json={"text": "why?", "anchor": None}
        )
        tid = thread["id"]
        with anyio.fail_after(10):
            await got.wait()
        # A claude-authored reply is internal: it must not trigger a notify.
        live_daemon.service.ws.post_message(tid, "the cache was cold", "claude")
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    assert len(seen) == 1
    content, meta = seen[0]
    assert "why?" in content
    assert meta["event"] == "thread.message"
    assert meta["thread"] == tid


async def test_pump_without_channel_does_not_steal(live_daemon, monkeypatch):
    monkeypatch.delenv("TN_CHANNEL", raising=False)
    seen: list[tuple[str, dict[str, Any]]] = []

    async def notify(content: str, meta: dict[str, Any]) -> None:
        seen.append((content, meta))

    pump = ChannelPump(live_daemon.url, notify)
    async with anyio.create_task_group() as tg:
        tg.start_soon(pump.run)
        await anyio.sleep(0.3)
        assert live_daemon.service.log.subscriber_count == 0  # fully idle
        await _post_json(f"{live_daemon.url}/api/threads", json={"text": "why?", "anchor": None})
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    assert seen == []
    # No heartbeat while disabled, and the pending hook can still deliver.
    assert not await _channel_active(live_daemon.url)
    claim = await _post_json(f"{live_daemon.url}/api/channel/claim", json={"consumer": "claude"})
    assert "why?" in claim["content"]  # not stolen
