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


def _text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# --- gate and capability detection -------------------------------------------


def test_gate_reads_tn_channel_env():
    assert ChannelGate(env={"TN_CHANNEL": "1"}).enabled
    assert not ChannelGate(env={"TN_CHANNEL": "0"}).enabled
    assert not ChannelGate(env={}).enabled


def test_gate_enable_notifies_listeners_once():
    gate = ChannelGate(env={})
    calls: list[int] = []
    gate.on_enable(lambda: calls.append(1))
    gate.enable()
    gate.enable()
    assert gate.enabled
    assert calls == [1]


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


def test_delivery_fires_ready_once_on_first_observe():
    delivery = ChannelDelivery(ChannelGate(env={}))
    calls: list[int] = []
    delivery.on_ready(lambda: calls.append(1))
    delivery.observe(_ctx_with(None))
    delivery.observe(_ctx_with(None))
    assert calls == [1]


async def test_notify_without_session_raises():
    delivery = ChannelDelivery(ChannelGate(env={}))
    with pytest.raises(RuntimeError):
        await delivery.notify("x", {})


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


async def _status(url: str) -> dict[str, Any]:
    return await _get_json(f"{url}/api/channel/status", params={"consumer": "claude"})


async def _ask(url: str, text: str = "why?") -> dict[str, Any]:
    return await _post_json(f"{url}/api/threads", json={"text": text, "anchor": None})


async def test_pump_delivers_only_after_ready_then_acks(live_daemon):
    seen: list[tuple[str, dict[str, Any]]] = []

    async def notify(content: str, meta: dict[str, Any]) -> None:
        seen.append((content, meta))

    pump = ChannelPump(live_daemon.url, notify, gate=ChannelGate(env={"TN_CHANNEL": "1"}))
    async with anyio.create_task_group() as tg:
        tg.start_soon(pump.run)
        # Connected as a channel bridge, but the MCP session is not observed yet.
        await _until(lambda: _status_is(live_daemon.url, "terminal"))
        thread = await _ask(live_daemon.url)  # the jxp window: asked before the handshake
        await anyio.sleep(0.3)
        assert seen == []
        pump.mark_ready()
        await _until(lambda: len(seen) == 1)
        await _until(lambda: _status_is(live_daemon.url, "live"))
        await _until(lambda: _delivered(live_daemon.url, 1))
        # A claude-authored reply is internal: no second delivery.
        live_daemon.service.ws.post_message(thread["id"], "the cache was cold", "claude")
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    assert len(seen) == 1
    content, meta = seen[0]
    assert "why?" in content
    assert meta["event"] == "thread.message"
    assert meta["thread"] == thread["id"]
    await _until(lambda: _status_is(live_daemon.url, "offline"))


async def _status_is(url: str, status: str) -> bool:
    return (await _status(url))["status"] == status


async def _delivered(url: str, seq: int) -> bool:
    return (await _status(url))["delivered_up_to"] >= seq


async def test_hook_mode_pump_is_present_but_never_steals(live_daemon):
    seen: list[tuple[str, dict[str, Any]]] = []

    async def notify(content: str, meta: dict[str, Any]) -> None:
        seen.append((content, meta))

    pump = ChannelPump(live_daemon.url, notify, gate=ChannelGate(env={}))
    async with anyio.create_task_group() as tg:
        tg.start_soon(pump.run)
        pump.mark_ready()
        await _until(lambda: _status_is(live_daemon.url, "terminal"))
        await _ask(live_daemon.url)
        await anyio.sleep(0.3)
        assert seen == []
        assert not (await _status(live_daemon.url))["channel_active"]
        claim = await _post_json(
            f"{live_daemon.url}/api/channel/claim", json={"consumer": "claude"}
        )
        assert "why?" in claim["content"]  # the hook still delivers
        tg.cancel_scope.cancel()


async def test_gate_opening_later_makes_pump_live(live_daemon):
    async def notify(content: str, meta: dict[str, Any]) -> None:
        pass

    gate = ChannelGate(env={})
    pump = ChannelPump(live_daemon.url, notify, gate=gate)
    async with anyio.create_task_group() as tg:
        tg.start_soon(pump.run)
        pump.mark_ready()
        await _until(lambda: _status_is(live_daemon.url, "terminal"))
        gate.enable()  # the client advertised claude/channel in the handshake
        await _until(lambda: _status_is(live_daemon.url, "live"))
        tg.cancel_scope.cancel()


async def test_failed_notify_is_redelivered_after_reconnect(live_daemon):
    attempts: list[str] = []

    async def notify(content: str, meta: dict[str, Any]) -> None:
        attempts.append(content)
        if len(attempts) == 1:
            raise RuntimeError("stdio closed")

    pump = ChannelPump(
        live_daemon.url, notify, gate=ChannelGate(env={"TN_CHANNEL": "1"}), backoff_s=0.05
    )
    pump.mark_ready()
    async with anyio.create_task_group() as tg:
        tg.start_soon(pump.run)
        await _ask(live_daemon.url)
        await _until(lambda: len(attempts) == 2)
        await _until(lambda: _delivered(live_daemon.url, 1))
        tg.cancel_scope.cancel()
    assert attempts[0] == attempts[1]
