"""Which Claude sessions are connected to the daemon, and whether any can take deliveries.

Each stdio bridge holds one `/ws/bridge` socket for its lifetime and reports its mode
(`hook`: the session sees UI events only via the UserPromptSubmit hook; `channel`: the
session accepts `notifications/claude/channel`) and readiness (its MCP session has been
observed, so it can actually send). In memory only: after a daemon restart bridges
reconnect and re-report.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from telemetry_nerd.core.consumer import kind_of
from telemetry_nerd.model.time import now_ms

log = logging.getLogger(__name__)

Mode = Literal["hook", "channel"]
Status = Literal["live", "terminal", "offline"]
MODES: frozenset[str] = frozenset({"hook", "channel"})


@dataclass
class _Bridge:
    consumer: str
    mode: Mode
    ready: bool
    since_ms: int

    @property
    def live(self) -> bool:
        return self.mode == "channel" and self.ready


class PresenceRegistry:
    def __init__(self, clock: Callable[[], int] = now_ms) -> None:
        self._clock = clock
        self._bridges: dict[int, _Bridge] = {}  # insertion order = connection order
        self._ids = itertools.count(1)
        self._subscribers: set[asyncio.Queue] = set()

    def connect(self, consumer: str, mode: Mode) -> int:
        conn = next(self._ids)
        self._bridges[conn] = _Bridge(consumer, mode, False, self._clock())
        self.changed(consumer)
        return conn

    def update(self, conn: int, *, mode: Mode | None = None, ready: bool | None = None) -> None:
        bridge = self._bridges[conn]
        if mode is not None:
            bridge.mode = mode
        if ready is not None:
            bridge.ready = ready
        self.changed(bridge.consumer)

    def disconnect(self, conn: int) -> None:
        bridge = self._bridges.pop(conn, None)
        if bridge is not None:
            self.changed(bridge.consumer)

    def connections(self, consumer: str) -> list[int]:
        return [c for c, b in self._bridges.items() if b.consumer == consumer]

    def live(self, consumer: str) -> bool:
        return self.deliverer(consumer) is not None

    def live_for_kind(self, kind: str) -> bool:
        """Any live bridge of this consumer kind, whatever its session id.

        The hook path uses this for its refusal check: a live channel bridge and a
        hook claim might name different per-session consumers, but both belong to the
        same Claude client kind — printing via the hook while a channel delivers would
        double-deliver (dtk).
        """
        return any(b.live for b in self._bridges.values() if kind_of(b.consumer) == kind)

    def deliverer(self, consumer: str) -> int | None:
        """The earliest-connected live bridge: the one that receives deliveries."""
        return next(
            (c for c, b in self._bridges.items() if b.consumer == consumer and b.live), None
        )

    def status(self, consumer: str) -> Status:
        return self.snapshot(consumer)["status"]

    def snapshot(self, consumer: str) -> dict:
        bridges = [b for b in self._bridges.values() if b.consumer == consumer]
        if not bridges:
            return {"status": "offline", "mode": None, "since_ms": None}
        best = next((b for b in bridges if b.live), bridges[0])
        status: Status = "live" if best.live else "terminal"
        return {"status": status, "mode": best.mode, "since_ms": best.since_ms}

    def sessions(self) -> list[dict]:
        """One entry per distinct consumer (per session, dtk), in connect order."""
        out: dict[str, dict] = {}
        for b in self._bridges.values():
            if b.consumer in out:
                continue
            out[b.consumer] = {
                "consumer": b.consumer,
                "kind": kind_of(b.consumer),
                **self.snapshot(b.consumer),
            }
        return list(out.values())

    # change fan-out -----------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def changed(self, consumer: str) -> None:
        """Tell subscribers `consumer`'s presence (or delivery cursor) changed."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(consumer)
            except asyncio.QueueFull:
                log.warning("presence subscriber queue full; dropping change for %s", consumer)
