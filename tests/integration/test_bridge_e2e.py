"""E2E: daemon subprocess + stdio bridge channel round trip (Task 11).

Starts a real `telemetry-nerd serve` subprocess against a testcontainer VictoriaMetrics,
then spawns `telemetry-nerd bridge` over stdio with TN_CHANNEL=1 using the MCP stdio
client transport and a notification binding. A user thread event must arrive as a
`notifications/claude/channel` notification within 5 s; Claude's `reply` through the
bridge lands in that thread and produces no second notification. Presence follows the
bridge (offline → live → offline), and a question asked before the handshake is delivered
exactly once (jxp).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.extension import ClientExtension, NotificationBinding
from pydantic import BaseModel

from telemetry_nerd.bridge.proxy import CHANNEL_NOTIFICATION

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon_url(tmp_path: Path, vm_url: str) -> Iterator[str]:
    """A real `telemetry-nerd serve` subprocess on a free port with a throwaway data dir."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "daemon.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "telemetry_nerd.cli",
                "serve",
                "--port",
                str(port),
                "--data-dir",
                str(tmp_path / "data"),
                "--source-url",
                vm_url,
            ],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if httpx.get(f"{url}/api/health", timeout=0.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if proc.poll() is not None:
                    raise RuntimeError(f"daemon exited early:\n{log_path.read_text()}")
                if time.monotonic() > deadline:
                    raise RuntimeError("daemon did not become healthy within 20 s")
                time.sleep(0.05)
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


class _ChannelParams(BaseModel):
    content: str
    meta: dict[str, str]


class _ChannelCollector(ClientExtension):
    """Collect `notifications/claude/channel` deliveries.

    The stdio client drops server notifications for methods unknown to the negotiated
    protocol version unless an extension binding claims the method, so the collector
    binds it explicitly rather than relying on `message_handler`.
    """

    identifier = "dev.telemetry-nerd/e2e"

    def __init__(self) -> None:
        self.events: list[_ChannelParams] = []
        self.received = anyio.Event()

    async def _on_channel(self, params: _ChannelParams) -> None:
        self.events.append(params)
        self.received.set()

    def notifications(self) -> tuple[NotificationBinding[_ChannelParams], ...]:
        return (
            NotificationBinding(
                method=CHANNEL_NOTIFICATION, params_type=_ChannelParams, handler=self._on_channel
            ),
        )


async def _until(predicate: Callable[[], Awaitable[bool]], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await anyio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


async def _get(url: str) -> Any:
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


async def _post(url: str, json: dict) -> Any:
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=json)
        r.raise_for_status()
        return r.json()


def _bridge_params(url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "telemetry_nerd.cli", "bridge", "--daemon-url", url],
        env={"TN_CHANNEL": "1"},
        cwd=REPO_ROOT,
    )


async def _status(url: str) -> str:
    return (await _get(f"{url}/api/channel/status?consumer=claude"))["status"]


async def test_channel_round_trip_through_stdio_bridge(daemon_url: str) -> None:
    url = daemon_url
    collector = _ChannelCollector()
    assert await _status(url) == "offline"
    async with Client(_bridge_params(url), mode="legacy", extensions=[collector]) as client:
        # TN_CHANNEL=1 and the handshake observed: the bridge reports a ready channel.
        async def live() -> bool:
            return await _status(url) == "live"

        await _until(live)

        thread = await _post(f"{url}/api/threads", json={"text": "why the dip?"})
        tid = thread["id"]
        with anyio.fail_after(5.0):
            await collector.received.wait()

        assert len(collector.events) == 1
        event = collector.events[0]
        assert "why the dip?" in event.content
        assert event.meta["event"] == "thread.message"
        assert event.meta["thread"] == tid

        # Claude replies through the bridge: the message lands in the thread...
        res = await client.call_tool("reply", {"thread": tid, "text": "the cache was cold"})
        assert not res.is_error
        snapshot = await _get(f"{url}/api/workspace")
        got = next(t for t in snapshot["threads"] if t["id"] == tid)
        texts = [m["text"] for m in got["messages"]]
        assert "the cache was cold" in texts
        assert any(m["author"] == "claude" for m in got["messages"])

        # ...and a claude-authored event is internal: no second notification.
        await anyio.sleep(0.5)
        assert len(collector.events) == 1

    async def offline() -> bool:
        return await _status(url) == "offline"

    await _until(offline)  # bridge exit closes its socket


async def test_question_asked_before_handshake_arrives_once(daemon_url: str) -> None:
    """jxp: an event pending before the MCP session exists is delivered, not dropped."""
    url = daemon_url
    await _post(f"{url}/api/threads", json={"text": "asked before the bridge started"})
    collector = _ChannelCollector()
    async with Client(_bridge_params(url), mode="legacy", extensions=[collector]):
        with anyio.fail_after(10.0):
            await collector.received.wait()
        await anyio.sleep(0.5)
        assert len(collector.events) == 1
        assert "asked before the bridge started" in collector.events[0].content
        status = await _get(f"{url}/api/channel/status?consumer=claude")
        assert status["delivered_up_to"] >= 1
        # The hook has nothing left to deliver.
        claim = await _post(f"{url}/api/channel/claim", json={"consumer": "claude"})
        assert claim["content"] is None
