"""Inline series handed over by the caller (Claude) to be drawn, with no source behind them.

Parsed once at the boundary into a `FetchResult` on a shared time grid; `TelemetryService.plot_inline`
stores it as fixed data (a code output of the pseudo node `INLINE_NODE`), so no path re-fetches
it or looks its expression up in a catalog."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import reduce
from itertools import pairwise

import pyarrow as pa

from telemetry_nerd.core.plot_table import PlotReason, PlotTableError
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import parse_time

INLINE_NODE = "inline"
INLINE_OUTPUT = "plot"
INLINE_SOURCE = "inline"
INLINE_EXPR = f"code:{INLINE_NODE}/{INLINE_OUTPUT}"
SAMPLE_OUTPUT = "sample"
SAMPLE_EXPR = f"code:{INLINE_NODE}/{SAMPLE_OUTPUT}"
MIN_STEP_MS = 1_000
MAX_GRID_BUCKETS = 20_000  # a grid finer than this makes the bucket-state pass needlessly slow


@dataclass(frozen=True)
class InlineSeries:
    label: str
    ts_ms: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class InlineData:
    result: FetchResult
    start_ms: int
    end_ms: int
    step_ms: int  # the largest step every timestamp is a whole number of steps after start_ms


def parse_inline_series(raw: list[dict], now_ms: int) -> list[InlineSeries]:
    """Validate `[{label, ts: [...], values: [...]}]`; `ts` entries are epoch ms or ISO-8601
    with timezone. Raises ValueError naming the offending series."""
    if not raw:
        raise ValueError("series must hold at least one series")
    out: list[InlineSeries] = []
    for i, s in enumerate(raw):
        label = str(s.get("label", "")).strip()
        if not label:
            raise ValueError(f"series[{i}] needs a label")
        if any(o.label == label for o in out):
            raise ValueError(f"series label {label!r} is used twice")
        ts, values = s.get("ts") or [], s.get("values") or []
        if not ts or len(ts) != len(values):
            raise ValueError(
                f"series {label!r}: ts and values must be non-empty and the same length "
                f"(got {len(ts)} and {len(values)})"
            )
        stamps = tuple(parse_time(str(t), now_ms) for t in ts)
        for at, (a, b) in enumerate(pairwise(stamps), start=1):
            if b <= a:
                raise PlotTableError(
                    PlotReason.TS_NOT_INCREASING,
                    f"series {label!r}: ts must be strictly increasing, index {at} does not "
                    f"increase",
                )
        for at, v in enumerate(values):
            if not (isinstance(v, int | float) and math.isfinite(v)):
                raise PlotTableError(
                    PlotReason.NOT_FINITE,
                    f"series {label!r}: values must be finite numbers, index {at} is {v!r}",
                )
        floats = tuple(float(v) for v in values)
        out.append(InlineSeries(label, stamps, floats))
    return out


def inline_data(series: list[InlineSeries]) -> InlineData:
    """The series as buckets (one sample per bucket: avg = min = max) on the coarsest grid
    holding every timestamp, anchored at the first timestamp rather than the epoch: an epoch
    anchor shrinks the step below the data's own interval, and every bucket without a sample
    breaks the drawn line. Refuses a grid that is too fine or too long to draw."""
    all_ts = [t for s in series for t in s.ts_ms]
    start, end = min(all_ts), max(all_ts)
    # One timestamp has no interval to measure.
    step = reduce(math.gcd, (t - start for t in all_ts)) or MIN_STEP_MS
    if step < MIN_STEP_MS:
        raise ValueError(
            f"timestamps share no grid coarser than {MIN_STEP_MS} ms (their common step is "
            f"{step} ms): round them to whole seconds"
        )
    if (end - start) // step + 1 > MAX_GRID_BUCKETS:
        raise ValueError(
            f"timestamps span {(end - start) // step + 1} steps of {step} ms, over the "
            f"{MAX_GRID_BUCKETS} limit: pass fewer or more regular points"
        )
    ids = {s.label: series_id(INLINE_SOURCE, {"series": s.label}) for s in series}
    rows = [(t, ids[s.label], v) for s in series for t, v in zip(s.ts_ms, s.values, strict=True)]
    buckets = pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "avg": [r[2] for r in rows],
            "min": [r[2] for r in rows],
            "max": [r[2] for r in rows],
            "count": [1] * len(rows),
        },
        schema=BUCKET_SCHEMA,
    )
    labels = pa.table(
        {
            "series_id": list(ids.values()),
            "labels": [labels_json({"series": label}) for label in ids],
        },
        schema=SERIES_SCHEMA,
    )
    return InlineData(FetchResult(buckets, labels), start, end, step)
