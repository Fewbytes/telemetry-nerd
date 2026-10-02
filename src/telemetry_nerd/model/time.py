"""Time primitives. All timestamps are int64 epoch milliseconds."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

_DURATION = re.compile(r"^(?:\d+(?:ms|s|m|h|d|w))+$")
_DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w)")
_UNIT_MS = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 604_800_000,
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def parse_duration(text: str) -> int:
    """`15s`, `5m`, and compound forms such as `1h30m` (Prometheus syntax)."""
    value = text.strip()
    if not _DURATION.match(value):
        raise ValueError(f"invalid duration {text!r}; use e.g. 15s, 5m, 1h, 1h30m, 2d")
    return sum(int(n) * _UNIT_MS[u] for n, u in _DURATION_PART.findall(value))


def format_duration(ms: int) -> str:
    for unit in ("w", "d", "h", "m", "s"):
        size = _UNIT_MS[unit]
        if ms >= size and ms % size == 0:
            return f"{ms // size}{unit}"
    return f"{ms}ms"


def parse_time(text: str, now_ms: int) -> int:
    """Parse `now`, `now-<duration>`, epoch milliseconds, or ISO-8601 with timezone."""
    value = text.strip()
    if value == "now":
        return now_ms
    if value.startswith("now-"):
        return now_ms - parse_duration(value[4:])
    if value.isdigit():
        return int(value)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {text!r} must include a timezone, e.g. +00:00")
    return int(parsed.timestamp() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TimeRange:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms:
            raise ValueError(
                f"time range end ({self.end_ms}) must be after start ({self.start_ms})"
            )

    def align(self, step_ms: int) -> TimeRange:
        """Expand outward to multiples of step."""
        start = (self.start_ms // step_ms) * step_ms
        end = -(-self.end_ms // step_ms) * step_ms
        return TimeRange(start, end)


@lru_cache(maxsize=16)
def zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz)


def check_timezone(tz: str) -> str:
    """The IANA name if it is one; ValueError otherwise (user input)."""
    try:
        zone(tz)
    except Exception as e:  # ZoneInfoNotFoundError, ValueError on malformed keys
        raise ValueError(
            f"unknown timezone {tz!r}: use an IANA name like Europe/Berlin or UTC"
        ) from e
    return tz
