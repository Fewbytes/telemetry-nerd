"""Requests in flight derived from access-log documents (pure, no I/O; telemetry-nerd-sgb.11).

Concurrency is an aggregate, not a document field: each document is one request spanning
[start, start + duration) (`timestamp: start`) or [end − duration, end) (`timestamp: end`), and
the number in flight at time u is the number of spans holding u. Per query bucket [t − step, t)
the adapter reports the exact time average of that count (avg) and its min and max inside the
bucket, from every document; nothing is sampled.

The time average uses the integral F(t) = ∫ L = Σ_{s<t} (t − s) − Σ_{e<t} (t − e) over sorted
starts s and ends e (prefix sums), so a bucket's average is (F(t) − F(t − step)) / step.

What it rests on (stated in the dataset's notes, never silently assumed):
- every request is logged exactly once (no sampling, no retry logged twice);
- the duration covers the span the timestamp anchors (the timer starts/stops where the log says);
- requests longer than `max_duration` that began (start) or ended (end) outside the documents read
  are missed; the longest duration seen says whether that happened;
- a request still running when queried has no document yet: the newest buckets undercount.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: documents read at most for one in_flight query: above it the query is refused, not truncated
MAX_DOCS = 100_000
#: documents per slice request (Elasticsearch's default index.max_result_window is 10 000; one
#: below it so a full page means documents arrived after the count, never a silent cut)
PAGE = 10_000
SLICE_DOCS = PAGE - 1


def read_range(
    start_ms: int, end_ms: int, step_ms: int, max_duration_ms: int, timestamp: str
) -> tuple[int, int]:
    """[lo, hi) of the time field to read for buckets ending start_ms..end_ms: the buckets'
    span plus max_duration on the side requests reach in from (before for start timestamps,
    after for end timestamps)."""
    lo, hi = start_ms - step_ms, end_ms
    return (lo - max_duration_ms, hi) if timestamp == "start" else (lo, hi + max_duration_ms)


def plan_slices(buckets: Sequence[tuple[int, int]], step_ms: int, cap: int = SLICE_DOCS) -> list:
    """Contiguous time slices [lo, hi) of at most `cap` documents, from (bucket start, count)
    pairs in ascending order. A single bucket over the cap raises ValueError."""
    out: list[tuple[int, int, int]] = []
    lo, n = None, 0
    for key, count in buckets:
        if count > cap:
            raise ValueError(f"{count} documents in one {step_ms} ms bucket (at most {cap})")
        if lo is not None and n + count > cap:
            out.append((lo, key, n))
            lo, n = None, 0
        if lo is None:
            lo = key
        n += count
    if lo is not None and n:
        out.append((lo, buckets[-1][0] + step_ms, n))
    return [s for s in out if s[2] > 0]


def spans(
    ts_ms: np.ndarray, duration: np.ndarray, unit_ms: float, timestamp: str
) -> tuple[np.ndarray, np.ndarray]:
    """(start, end) in ms of each request."""
    d = duration * unit_ms
    return (ts_ms, ts_ms + d) if timestamp == "start" else (ts_ms - d, ts_ms)


@dataclass(frozen=True)
class InFlight:
    ends: np.ndarray  # bucket END times with a value (the documents' coverage, see `derive`)
    avg: np.ndarray  # exact time average of the number in flight over [t − step, t)
    min: np.ndarray
    max: np.ndarray


def derive(start: np.ndarray, end: np.ndarray, grid_ends: np.ndarray, step_ms: int) -> InFlight:
    """Per bucket [t − step, t) of `grid_ends`: the time average, min and max of the number of
    spans [start, end) holding each instant. Buckets before the earliest start or after the
    latest end are left out (outside the documents' coverage: unknown, not zero)."""
    empty = np.array([], float)
    if start.size == 0 or grid_ends.size == 0:
        return InFlight(np.array([], np.int64), empty, empty, empty)
    origin = float(grid_ends[0] - step_ms)  # relative times keep the prefix sums exact enough
    s = np.sort(start.astype(float) - origin)
    e = np.sort(end.astype(float) - origin)
    cs, ce = np.r_[0.0, np.cumsum(s)], np.r_[0.0, np.cumsum(e)]

    def integral(t: np.ndarray) -> np.ndarray:
        ns, ne = np.searchsorted(s, t, "left"), np.searchsorted(e, t, "left")
        return ns * t - cs[ns] - (ne * t - ce[ne])

    def level(t: np.ndarray) -> np.ndarray:
        return np.searchsorted(s, t, "right") - np.searchsorted(e, t, "right")

    b = grid_ends.astype(float) - origin
    a = b - step_ms
    keep = (b > s[0]) & (a < e[-1])
    a, b, ends = a[keep], b[keep], grid_ends[keep]
    avg = (integral(b) - integral(a)) / step_ms
    events = np.unique(np.r_[s, e])
    at_events = level(events)
    lo = np.searchsorted(events, a, "right")
    hi = np.searchsorted(events, b, "left")
    first = level(a)
    mn, mx = first.astype(float), first.astype(float)
    for i in np.flatnonzero(hi > lo):
        seg = at_events[lo[i] : hi[i]]
        mn[i], mx[i] = min(mn[i], seg.min()), max(mx[i], seg.max())
    return InFlight(ends.astype(np.int64), avg, mn, mx)
