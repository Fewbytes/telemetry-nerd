"""Source adapter contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.model.distribution import DistResult
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.semantics import MissingDataSemantics


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
    max_metrics: int = 200_000
    discover_timeout_s: float = 120.0  # name/metadata listings are large (Wikimedia: ~9 MB)


class Source(Protocol):
    name: str
    identity: str  # stable id of what this source reads (flavor, endpoint, resolution)
    resolution_ms: int
    #: what the backend does at the edges of its data; None for sources without a profile
    semantics: MissingDataSemantics | None

    async def fetch(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult: ...

    async def probe(self) -> dict: ...

    async def fetch_values(self, expr: str, rng: TimeRange, step_ms: int) -> FetchResult: ...

    async def fetch_histogram(
        self, selector: str, by: Sequence[str], rng: TimeRange, step_ms: int
    ) -> DistResult: ...

    async def discover(self) -> Discovery: ...

    async def scrape_interval(self, selector: str, at_ms: int | None = None) -> int | None: ...
