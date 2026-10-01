"""Source adapter contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from telemetry_nerd.model.distribution import DistResult
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import TimeRange


class SourceError(Exception):
    """Typed source failure. `hint` tells Claude how to recover."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class LimitExceeded(SourceError):
    pass


class SourceUnavailable(SourceError):
    pass


@dataclass(frozen=True)
class Limits:
    max_series: int = 500
    max_points: int = 2_000_000
    timeout_s: float = 30.0


class Source(Protocol):
    name: str
    identity: str  # stable id of what this source reads (flavor, endpoint, resolution)
    resolution_ms: int

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult: ...

    async def probe(self) -> dict: ...

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult: ...

    async def fetch_histogram(
        self, selector: str, by: Sequence[str], rng: TimeRange, step_ms: int
    ) -> DistResult: ...
