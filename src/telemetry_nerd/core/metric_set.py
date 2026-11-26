"""Metric set input: several caller-supplied series drawn together, one panel per member.

Spec: design-docs/ui-layer-and-compute-server.md 6. Each member is parsed with the same boundary
code as a single `plot_data` call; nothing is resampled onto another member's grid, so each
member's step and coverage are stated instead."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telemetry_nerd.core.inline_series import (
    InlineSeries,
    inline_grid,
    parse_inline_series,
)
from telemetry_nerd.core.plot_csv import csv_inline_series
from telemetry_nerd.core.plot_table import (
    PlotReason,
    PlotTableError,
    check_label,
    check_line_series_count,
    over_limit,
    parse_plot_columns,
    parse_plot_matrix,
    with_time_axis,
)
from telemetry_nerd.model.time import format_duration, iso

MAX_SET_MEMBERS = 6
METRIC_SET_KIND = "metric_set"


@dataclass(frozen=True)
class SetMember:
    name: str
    role: str | None
    unit: str | None
    series: list[InlineSeries]
    start_ms: int
    end_ms: int
    step_ms: int

    def describe(self) -> list[str]:
        """Role notes the agent reads: role, then per series n and missing (grid slots inside the
        member's coverage with no value), then the member's own step and coverage."""
        slots = (self.end_ms - self.start_ms) // self.step_ms + 1
        notes = [f"role: {self.role}"] if self.role else []
        notes += [
            f"{s.label}: n={len(s.values)}, missing={slots - len(s.values)}" for s in self.series
        ]
        notes.append(
            f"step {format_duration(self.step_ms)}, coverage "
            f"{iso(self.start_ms)} to {iso(self.end_ms)}"
        )
        return notes


def parse_metric_set(raw: list[dict[str, Any]], now_ms: int) -> list[SetMember]:
    """Validate `[{name, role?, series | data, unit?}]`; `series` and `data` take the forms of
    plot_data. Raises PlotTableError (over_limit past MAX_SET_MEMBERS, or the member's own
    refusal prefixed by its name) before anything is drawn."""
    if not raw:
        raise PlotTableError(PlotReason.EMPTY, "set must hold at least one member")
    if len(raw) > MAX_SET_MEMBERS:
        raise over_limit("members per set (split into two sets)", MAX_SET_MEMBERS, len(raw), None)
    members: list[SetMember] = []
    for i, m in enumerate(raw):
        name = str(m.get("name", "")).strip()
        check_label(name, f"set member {i}", [x.name for x in members])
        if (m.get("series") is None) == (m.get("data") is None):
            raise PlotTableError(
                PlotReason.KIND_SHAPE_MISMATCH,
                f"set member {name!r}: pass exactly one of series or data",
            )
        try:
            series, stamps = _member_series(m, now_ms)
            check_line_series_count(len(series))
            start, end, step = inline_grid(stamps)
        except PlotTableError as e:
            reason_prefix = f"{e.reason.value}: "
            raise PlotTableError(
                e.reason, f"set member {name!r}: {str(e).removeprefix(reason_prefix)}"
            ) from e
        except ValueError as e:
            raise ValueError(f"set member {name!r}: {e}") from e
        members.append(SetMember(name, m.get("role"), m.get("unit"), series, start, end, step))
    return members


def _member_series(m: dict[str, Any], now_ms: int) -> tuple[list[InlineSeries], list[int]]:
    """The member's lines and every timestamp it sampled, a null row included: a missing value is
    a gap on the member's own step, not a coarser step."""
    if m.get("series") is not None:
        series = parse_inline_series(m["series"], now_ms)
        return series, [t for s in series for t in s.ts_ms]
    data = m["data"]
    table = (
        parse_plot_columns(data, time_column=True)
        if isinstance(data, dict)
        else parse_plot_matrix(data, time_column=True)
    )
    table = with_time_axis(table)
    assert table.time_ms is not None
    return csv_inline_series(table), list(table.time_ms)
