"""Channel delivery: watch daemon events, claim intentional ones, notify the session.

The pump claims and heartbeats only while a `ChannelGate` is open — evidence that
this Claude session loaded the bridge as a channel (TN_CHANNEL=1, or the client
advertised the claude/channel capability in the MCP handshake). While the gate is
closed nothing is claimed, so the UserPromptSubmit hook delivers instead.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
import websockets

log = logging.getLogger(__name__)

HEARTBEAT_S = 20.0
"""Claim/heartbeat cadence while delivery is enabled (channel_active window is 60 s)."""

BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
HTTP_TIMEOUT_S = 5.0

Notify = Callable[[str, dict[str, Any]], Awaitable[None]]


class ChannelGate:
    """Whether this session should receive channel deliveries (claim + heartbeat).

    Opened by evidence: `TN_CHANNEL=1` in the environment at construction, or the
    client advertising the claude/channel capability during the MCP handshake
    (the bridge calls `enable()` then). One-way: once open it never closes.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self._enabled = env.get("TN_CHANNEL") == "1"
        self._event: anyio.Event | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        if not self._enabled:
            log.info("channel delivery enabled")
        self._enabled = True
        if self._event is not None:
            self._event.set()

    async def wait_enabled(self) -> None:
        """Block until the gate is open (returns immediately when already open)."""
        if self._enabled:
            return
        if self._event is None:
            self._event = anyio.Event()
        await self._event.wait()


class ChannelPump:
    """Delivers user workspace events from the daemon to the Claude session.

    Connects to the daemon's `/ws` from the current `last_seq`; on each
    intentional event claims the pending batch over HTTP and calls
    `notify(content, meta)`. Heartbeats every `heartbeat_s` so
    `/api/channel/status` reports the channel active. Reconnects with backoff.
    """

    def __init__(
        self,
        daemon_url: str,
        notify: Notify,
        consumer: str = "claude",
        *,
        gate: ChannelGate | None = None,
        heartbeat_s: float = HEARTBEAT_S,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self._daemon_url = daemon_url.rstrip("/")
        self._notify = notify
        self._consumer = consumer
        self._gate = gate if gate is not None else ChannelGate()
        self._heartbeat_s = heartbeat_s
        self._backoff_s = backoff_s

    async def run(self) -> None:
        """Run until cancelled; claims and heartbeats only while the gate is open."""
        await self._gate.wait_enabled()
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._heartbeat_loop)
            await self._connect_loop()

    async def _connect_loop(self) -> None:
        backoff = self._backoff_s
        while True:
            try:
                since = await self._last_seq()
                async with websockets.connect(
                    self._ws_url(since), open_timeout=HTTP_TIMEOUT_S
                ) as ws:
                    backoff = self._backoff_s
                    await self._watch(ws)
            except (
                websockets.WebSocketException,
                httpx.HTTPError,
                OSError,
                ValueError,
                KeyError,
            ) as e:
                log.warning("channel pump: %s; reconnecting in %.1fs", e, backoff)
                await anyio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _watch(self, ws: Any) -> None:
        async for message in ws:
            event = json.loads(message)
            if event.get("klass") == "intentional":
                content, meta = await self._claim()
                if content is not None:
                    await self._notify(content, meta)

    async def _heartbeat_loop(self) -> None:
        # Beat immediately on start so /api/channel/status flips to active before
        # the first 20 s window; a fresh active channel keeps the pending hook away.
        while True:
            try:
                await self._post("/api/channel/heartbeat", {"consumer": self._consumer})
            except (httpx.HTTPError, OSError) as e:
                log.debug("heartbeat failed: %s", e)
            await anyio.sleep(self._heartbeat_s)

    async def _claim(self) -> tuple[str | None, dict[str, Any]]:
        data = await self._post("/api/channel/claim", {"consumer": self._consumer})
        return data.get("content"), data.get("meta") or {}

    async def _last_seq(self) -> int:
        data = await self._get("/api/health")
        return int(data["last_seq"])

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._daemon_url, timeout=HTTP_TIMEOUT_S) as client:
            r = await client.post(path, json=body)
            r.raise_for_status()
            return r.json()

    async def _get(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._daemon_url, timeout=HTTP_TIMEOUT_S) as client:
            r = await client.get(path)
            r.raise_for_status()
            return r.json()

    def _ws_url(self, since: int) -> str:
        parts = urlsplit(self._daemon_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/ws?since={since}"
