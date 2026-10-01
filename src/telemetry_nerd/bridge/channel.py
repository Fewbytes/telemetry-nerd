"""Channel delivery: hold the daemon's `/ws/bridge` socket, report presence, send deliveries.

The pump connects as soon as the bridge starts, in every mode, so the UI can tell a
running Claude session from none. It reports its mode (`channel` once a `ChannelGate` is
open — TN_CHANNEL=1, or the client advertised the claude/channel capability — else
`hook`) and `ready` once the MCP session has been observed. The daemon delivers only to a
ready channel bridge; each `deliver` is acked after the notification is sent, and only the
ack advances the daemon's cursor, so nothing is claimed that cannot be sent.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
import websockets

log = logging.getLogger(__name__)

BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
OPEN_TIMEOUT_S = 5.0

Notify = Callable[[str, dict[str, Any]], Awaitable[None]]


class ChannelGate:
    """Whether this session accepts channel notifications.

    Opened by evidence: `TN_CHANNEL=1` in the environment at construction, or the
    client advertising the claude/channel capability during the MCP handshake
    (the bridge calls `enable()` then). One-way: once open it never closes.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self._enabled = env.get("TN_CHANNEL") == "1"
        self._listeners: list[Callable[[], None]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def on_enable(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def enable(self) -> None:
        if self._enabled:
            return
        log.info("channel delivery enabled")
        self._enabled = True
        for listener in self._listeners:
            listener()


class ChannelPump:
    """Presence reports and channel deliveries over the daemon's `/ws/bridge`.

    Reconnects with backoff; every (re)connect re-reports mode and readiness, and the
    daemon resends any delivery that was never acked.
    """

    def __init__(
        self,
        daemon_url: str,
        notify: Notify,
        consumer: str = "claude",
        *,
        gate: ChannelGate | None = None,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self._daemon_url = daemon_url.rstrip("/")
        self._notify = notify
        self._consumer = consumer
        self._gate = gate if gate is not None else ChannelGate()
        self._backoff_s = backoff_s
        self._ready = False
        self._changed = anyio.Event()
        self._gate.on_enable(self._kick)

    def mark_ready(self) -> None:
        """The MCP session exists: notifications can be sent from now on."""
        if not self._ready:
            self._ready = True
            self._kick()

    def _kick(self) -> None:
        self._changed.set()

    async def run(self) -> None:
        """Run until cancelled."""
        backoff = self._backoff_s
        while True:
            try:
                async with websockets.connect(self._ws_url(), open_timeout=OPEN_TIMEOUT_S) as ws:
                    backoff = self._backoff_s
                    await self._session(ws)
            except (websockets.WebSocketException, OSError, ValueError, KeyError) as e:
                log.warning("channel pump: %s; reconnecting in %.1fs", e, backoff)
            await anyio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _session(self, ws: Any) -> None:
        async with anyio.create_task_group() as tg:

            async def report() -> None:
                while True:
                    self._changed = anyio.Event()
                    await self._report(ws)
                    await self._changed.wait()

            tg.start_soon(report)
            try:
                await self._deliveries(ws)
            finally:
                tg.cancel_scope.cancel()

    async def _report(self, ws: Any) -> None:
        mode = "channel" if self._gate.enabled else "hook"
        await ws.send(json.dumps({"type": "hello", "mode": mode}))
        if self._ready:
            await ws.send(json.dumps({"type": "ready"}))

    async def _deliveries(self, ws: Any) -> None:
        async for message in ws:
            frame = json.loads(message)
            if frame.get("type") != "deliver":
                log.warning("channel pump: ignoring unknown frame %r", frame.get("type"))
                continue
            try:
                await self._notify(frame["content"], frame.get("meta") or {})
            except (
                RuntimeError,
                OSError,
                anyio.ClosedResourceError,
                anyio.BrokenResourceError,
            ) as e:
                # Unacked: drop the socket so the daemon requeues it for the next connect.
                log.error("channel notify failed: %s; reconnecting to redeliver", e)
                return
            await ws.send(json.dumps({"type": "ack", "up_to": frame["up_to"]}))

    def _ws_url(self) -> str:
        parts = urlsplit(self._daemon_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/ws/bridge?{urlencode({'consumer': self._consumer})}"
