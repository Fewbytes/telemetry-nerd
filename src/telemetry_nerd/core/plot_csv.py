"""CSV text handed over by the caller (Claude), parsed into a `PlotTable` (spec 2.2, `plot_csv`).

There is no file or URL input: the text is the tool argument, so nothing is fetched or read."""

from __future__ import annotations

import csv
import io
from itertools import pairwise

from telemetry_nerd.core.inline_series import InlineSeries
from telemetry_nerd.core.plot_table import (
    MAX_CSV_BYTES,
    MAX_POINTS_PER_TIME_SERIES,
    PlotColumn,
    PlotReason,
    PlotTable,
    PlotTableError,
    check_column_count,
    check_label,
    number,
    over_limit,
)
from telemetry_nerd.model.time import parse_time

TIME_COLUMN_NAMES = frozenset({"ts", "time", "timestamp"})
REFUSED_DELIMITERS = {";": "semicolon", "\t": "tab"}


def parse_plot_csv(text: str, now_ms: int) -> PlotTable:
    """First row is the header, delimiter `,`. A `ts`/`time`/`timestamp` column is the time axis
    (epoch ms or ISO-8601 with timezone); a column with a non-numeric cell is the label column
    (at most one); the rest are numeric and an empty cell is a missing value."""
    size = len(text.encode("utf-8"))
    if size > MAX_CSV_BYTES:
        raise over_limit("CSV size in bytes", MAX_CSV_BYTES, size, None)
    rows = _read_rows(text)
    header, body = rows[0], rows[1:]
    if not body:
        raise PlotTableError(PlotReason.EMPTY, "CSV has a header but no data rows")
    if len(body) > MAX_POINTS_PER_TIME_SERIES:
        raise over_limit("points per series", MAX_POINTS_PER_TIME_SERIES, len(body), None)
    for r, row in enumerate(body, start=2):
        if len(row) != len(header):
            raise PlotTableError(
                PlotReason.RAGGED, f"CSV row {r} has {len(row)} cells, the header has {len(header)}"
            )
    names = [h.strip() for h in header]
    seen: list[str] = []
    for c, name in enumerate(names):
        check_label(name, f"CSV column {c}", seen)
        seen.append(name)

    time_idx = [c for c, n in enumerate(names) if n.lower() in TIME_COLUMN_NAMES]
    if len(time_idx) > 1:
        raise PlotTableError(PlotReason.CSV_MALFORMED, "CSV has more than one time column")
    cells = [[row[c].strip() for row in body] for c in range(len(names))]
    label_idx = [
        c
        for c in range(len(names))
        if c not in time_idx and not all(map(_is_number_or_empty, cells[c]))
    ]
    if len(label_idx) > 1:
        raise PlotTableError(
            PlotReason.CSV_MALFORMED,
            f"CSV has more than one non-numeric column: {[names[c] for c in label_idx]}",
        )
    numeric_idx = [c for c in range(len(names)) if c not in time_idx and c not in label_idx]
    if not numeric_idx:
        raise PlotTableError(PlotReason.EMPTY, "CSV has no numeric column")
    check_column_count(len(numeric_idx))

    columns = tuple(
        PlotColumn(
            names[c],
            tuple(
                None if v == "" else number(float(v), f"column {names[c]!r}, CSV row {r}")
                for r, v in enumerate(cells[c], start=2)
            ),
        )
        for c in numeric_idx
    )
    for column in columns:
        if all(v is None for v in column.values):
            raise PlotTableError(PlotReason.EMPTY, f"column {column.label!r} has no values")
    row_labels = tuple(cells[label_idx[0]]) if label_idx else None
    time_ms = _time_axis(cells[time_idx[0]], now_ms) if time_idx else None
    return PlotTable(columns, row_labels, time_ms)


def csv_inline_series(table: PlotTable) -> list[InlineSeries]:
    """One line per numeric column; an empty cell is omitted so the line shows a gap there."""
    if table.time_ms is None:
        raise PlotTableError(
            PlotReason.KIND_SHAPE_MISMATCH,
            "a line needs a time column named ts, time or timestamp",
        )
    return [
        InlineSeries(
            c.label,
            tuple(t for t, v in zip(table.time_ms, c.values, strict=True) if v is not None),
            tuple(v for v in c.values if v is not None),
        )
        for c in table.columns
    ]


def _read_rows(text: str) -> list[list[str]]:
    first_line = text.lstrip("﻿").split("\n", 1)[0]
    if first_line.lower().startswith("sep="):
        raise PlotTableError(
            PlotReason.CSV_UNSUPPORTED_DELIMITER, "a leading sep= line is not supported; use ','"
        )
    try:
        rows = [r for r in csv.reader(io.StringIO(text.lstrip("﻿")), strict=True) if r]
    except csv.Error as e:
        raise PlotTableError(PlotReason.CSV_MALFORMED, f"CSV is malformed: {e}") from e
    if not rows:
        raise PlotTableError(PlotReason.EMPTY, "CSV is empty")
    if len(rows[0]) == 1:
        for delimiter, name in REFUSED_DELIMITERS.items():
            if delimiter in rows[0][0]:
                raise PlotTableError(
                    PlotReason.CSV_UNSUPPORTED_DELIMITER,
                    f"the header looks {name}-delimited; only ',' is supported",
                )
    return rows


def _is_number_or_empty(cell: str) -> bool:
    if cell == "":
        return True
    try:
        float(cell)
    except ValueError:
        return False
    return True


def _time_axis(cells: list[str], now_ms: int) -> tuple[int, ...]:
    stamps: list[int] = []
    for r, cell in enumerate(cells, start=2):
        try:
            stamps.append(parse_time(cell, now_ms))
        except ValueError as e:
            raise PlotTableError(
                PlotReason.TS_UNPARSEABLE, f"time cell {cell!r} in CSV row {r}: {e}"
            ) from e
    for r, (a, b) in enumerate(pairwise(stamps), start=3):
        if b <= a:
            raise PlotTableError(
                PlotReason.TS_NOT_INCREASING, f"time in CSV row {r} does not increase"
            )
    return tuple(stamps)
