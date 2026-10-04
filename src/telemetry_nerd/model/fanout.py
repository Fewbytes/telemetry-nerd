"""Live fan-out to bounded asyncio queues, safe to publish from any thread.

Sync MCP tools run in worker threads, and asyncio.Queue is not thread-safe: each queue
remembers the loop it was subscribed on, and a publish from another thread hands the put to
that loop (`call_soon_threadsafe`). A full queue drops the item with a warning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class Fanout[T]:
    def __init__(self, maxsize: int, on_full: Callable[[T], None]) -> None:
        self._maxsize = maxsize
        self._on_full = on_full
        self._queues: dict[asyncio.Queue, asyncio.AbstractEventLoop | None] = {}

    def __len__(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues[queue] = _running_loop()  # None outside a loop: puts happen in place
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.pop(queue, None)

    def publish(self, item: T) -> None:
        here = _running_loop()
        for queue, loop in list(self._queues.items()):
            if loop is None or loop is here:
                self._put(queue, item)
                continue
            try:
                loop.call_soon_threadsafe(self._put, queue, item)
            except RuntimeError:  # the subscriber's loop is closed: nobody is listening
                self._queues.pop(queue, None)

    def _put(self, queue: asyncio.Queue, item: T) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            self._on_full(item)
