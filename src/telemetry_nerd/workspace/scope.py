"""The active workspace id, with per-context pinning (spec D2/D3).

Unpinned reads follow the active id; `pinned()`/`using()` fix the id for the current
context (tasks created inside inherit the pin, since a ContextVar is copied at creation).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from telemetry_nerd.model.fanout import Fanout

log = logging.getLogger(__name__)


class ActiveWorkspace:
    def __init__(self, initial: str) -> None:
        self._active = initial
        self._lock = threading.Lock()
        self._pin: ContextVar[str | None] = ContextVar("workspace_pin", default=None)
        self._subscribers: Fanout[dict] = Fanout(
            100, lambda f: log.warning("workspace subscriber queue full; dropping frame %s", f)
        )

    @property
    def active(self) -> str:
        with self._lock:
            return self._active

    def __call__(self) -> str:
        pin = self._pin.get()
        return pin if pin is not None else self.active

    @contextmanager
    def using(self, wid: str) -> Iterator[None]:
        token = self._pin.set(wid)
        try:
            yield
        finally:
            self._pin.reset(token)

    def pinned(self):
        """Pin the id read right now (the active one, or the enclosing pin)."""
        return self.using(self())

    def set_active(self, wid: str) -> None:
        with self._lock:
            self._active = wid

    # change fan-out -----------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        return self._subscribers.subscribe()

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.unsubscribe(queue)

    def notify(self, frame: dict) -> None:
        """Fan a frame out to subscribers, from any thread (each queue is fed on its loop)."""
        self._subscribers.publish(frame)
