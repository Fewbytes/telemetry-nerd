"""Per-source politeness: bounded concurrency and minimum spacing between request starts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class Gate:
    def __init__(
        self,
        max_concurrency: int = 4,
        min_interval_ms: int = 0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._spacing = asyncio.Lock()
        self._interval = min_interval_ms / 1000
        self._clock = clock
        self._sleep = sleep
        self._next_start: float | None = None

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._sem:
            if self._interval:
                async with self._spacing:
                    if self._next_start is not None:
                        wait = self._next_start - self._clock()
                        if wait > 0:
                            await self._sleep(wait)
                    self._next_start = self._clock() + self._interval
            yield
