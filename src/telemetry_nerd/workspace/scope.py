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

log = logging.getLogger(__name__)


class ActiveWorkspace:
    def __init__(self, initial: str) -> None:
        self._active = initial
        self._lock = threading.Lock()
        self._pin: ContextVar[str | None] = ContextVar("workspace_pin", default=None)
        self._subscribers: set[asyncio.Queue] = set()

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
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def notify(self, frame: dict) -> None:
        """Fan a frame out to subscribers. asyncio.Queue is not thread-safe: call this on
        the event-loop thread only."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                log.warning("workspace subscriber queue full; dropping frame %s", frame)
