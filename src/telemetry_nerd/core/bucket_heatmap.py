"""Histogram-derived heatmap input: a series of histograms over time as time x bucket counts.

Spec: design-docs/ui-layer-and-compute-server.md 5.1. The source's bucket boundaries are kept (no
rebinning). Cumulative sources (Prometheus classic `_bucket` counters) are differenced twice: across
`le` to per-bucket counts, then across time to counts per interval. A counter reset (any count that
falls) is a gap in that cell, never a negative count."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise

from telemetry_nerd.core.plot_table import PlotReason, PlotTable, PlotTableError


def bucket_labels(bounds: Sequence[float | None]) -> list[str]:
    """One row label per bucket, `(lo, hi]`; a `None` bound is +Inf (JSON cannot carry it)."""
    edges = [math.inf if b is None else b for b in bounds]
    for i, (lo, hi) in enumerate(pairwise(edges), start=1):
        if not hi > lo:
            raise PlotTableError(
                PlotReason.TS_NOT_INCREASING, f"bounds must be strictly increasing, index {i}"
            )
    if any(math.isnan(e) or e == -math.inf for e in edges[:-1]) or edges[-1] == -math.inf:
        raise PlotTableError(PlotReason.NOT_FINITE, "bounds must be numbers (null is +Inf)")
    labels = [f"<= {edges[0]:g}"]
    labels += [f"({lo:g}, {hi:g}]" for lo, hi in pairwise(edges)]
    return labels


def bucket_heatmap_columns(
    table: PlotTable, bounds: Sequence[float | None], cumulative: bool
) -> tuple[dict[str, list[float | None]], list[str]]:
    """Heatmap columns (one per interval, labelled by its end time) and row labels (one per bucket).

    `table` has `time_ms` and one column per bucket in `bounds` order. With `cumulative`, each
    snapshot is Prometheus-classic (cumulative across buckets and over time) and the first one only
    anchors the first interval; otherwise each snapshot already holds per-bucket counts."""
    rows = bucket_labels(bounds)
    if len(table.columns) != len(bounds):
        raise PlotTableError(
            PlotReason.RAGGED,
            f"{len(bounds)} bounds but {len(table.columns)} bucket columns: one column per bound",
        )
    assert table.time_ms is not None
    snapshots = [[col.values[t] for col in table.columns] for t in range(len(table.time_ms))]
    if not cumulative:
        for t, snap in enumerate(snapshots):
            if any(v is not None and v < 0 for v in snap):
                raise PlotTableError(
                    PlotReason.NOT_FINITE,
                    f"row {t}: a bucket count is negative; if the buckets are cumulative, "
                    "pass cumulative=true",
                )
        return {_stamp(ms): snap for ms, snap in zip(table.time_ms, snapshots, strict=True)}, rows
    if len(snapshots) < 2:
        raise PlotTableError(
            PlotReason.EMPTY, "cumulative buckets need at least 2 snapshots to difference"
        )
    per_bucket = [_difference_across_le(s) for s in snapshots]
    columns: dict[str, list[float | None]] = {}
    for t in range(1, len(per_bucket)):
        prev, cur = per_bucket[t - 1], per_bucket[t]
        columns[_stamp(table.time_ms[t])] = [
            None if a is None or b is None or b < a else b - a
            for a, b in zip(prev, cur, strict=True)
        ]
    return columns, rows


def _difference_across_le(snapshot: list[float | None]) -> list[float | None]:
    """Cumulative-over-`le` counts to per-bucket counts; a bucket whose cumulative falls is a gap."""
    out: list[float | None] = []
    previous: float | None = 0.0
    for v in snapshot:
        out.append(None if v is None or previous is None or v < previous else v - previous)
        previous = v
    return out


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
