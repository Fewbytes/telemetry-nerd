"""Plot mode input: numbers handed over by the caller (Claude), parsed once into a `PlotTable`.

Spec: design-docs/visualization-service-for-claude.md sections 2.2 to 2.4. Everything after
the parsers takes the validated `PlotTable`, never the raw JSON. A failure raises
`PlotTableError` with a stable `reason`; a limit is refused, never truncated (principle 4).

Reasons the spec lists for time axes, CSV, kinds and forms (`ts_*`, `grid_*`, `csv_*`,
`unknown_kind`, `kind_shape_mismatch`, `refused_form`) join `PlotReason` with the card that
raises them."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

# Limits of spec 2.3: one source of truth, echoed in every over_limit message.
MAX_SERIES_PER_PANEL = 8
MAX_POINTS_PER_TIME_SERIES = 100_000
MAX_VALUES_PER_SAMPLE = 1_000_000
MAX_BARS = 50
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_LABEL_CHARS = 80

# Spec section 4: a hard rule with no flag or override. The only file under src/ and ui/src that may
# name these forms; tests/unit/test_no_pie_guard.py scans for any other.
PIE_LIKE_KINDS = frozenset({"pie", "donut", "doughnut", "radial", "sunburst", "3d_pie"})
PIE_REFUSAL = (
    "Pie charts are never rendered: angle and area are read less accurately than length, and "
    "slices are hard to compare. Use `kind=bar` (sorted, zero baseline) for parts of a whole."
)
DRAWABLE_KINDS = ("line",)

ANONYMOUS_COLUMN = "values"
MATRIX_COLUMN_PREFIX = "c"


class PlotReason(StrEnum):
    EMPTY = "empty"
    RAGGED = "ragged"
    NOT_FINITE = "not_finite"
    DUPLICATE_LABEL = "duplicate_label"
    MISSING_LABEL = "missing_label"
    OVER_LIMIT = "over_limit"
    TS_NOT_INCREASING = "ts_not_increasing"
    TS_UNPARSEABLE = "ts_unparseable"
    CSV_UNSUPPORTED_DELIMITER = "csv_unsupported_delimiter"
    CSV_MALFORMED = "csv_malformed"
    KIND_SHAPE_MISMATCH = "kind_shape_mismatch"
    UNKNOWN_KIND = "unknown_kind"
    REFUSED_FORM = "refused_form"


class PlotTableError(ValueError):
    """Invalid caller data; `reason` is the stable code, `str(e)` the sentence for the caller."""

    def __init__(self, reason: PlotReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PlotColumn:
    label: str
    values: tuple[float | None, ...]  # None is a missing value (JSON null)


@dataclass(frozen=True)
class PlotTable:
    columns: tuple[PlotColumn, ...]
    row_labels: tuple[str, ...] | None  # one per row (bar category, matrix row), if supplied
    time_ms: tuple[int, ...] | None = None  # one per row, strictly increasing: a time axis

    @property
    def row_count(self) -> int:
        return len(self.columns[0].values)


def parse_plot_array(values: object, labels: object = None) -> PlotTable:
    """`[1.2, 3.4, ...]`: one anonymous column named `values`."""
    return parse_plot_columns({ANONYMOUS_COLUMN: values}, labels)


def parse_plot_columns(columns: Mapping[str, object], labels: object = None) -> PlotTable:
    """`{"a": [...], "b": [...]}`: every column the same length; keys are the labels."""
    if not columns:
        raise PlotTableError(PlotReason.EMPTY, "data holds no columns")
    check_column_count(len(columns))
    parsed: list[PlotColumn] = []
    for label, raw in columns.items():
        check_label(label, f"column {label!r}", [c.label for c in parsed])
        if not isinstance(raw, Sequence) or isinstance(raw, str):
            raise PlotTableError(
                PlotReason.NOT_FINITE, f"column {label!r} must be a list of numbers"
            )
        if not raw:
            raise PlotTableError(PlotReason.EMPTY, f"column {label!r} is empty")
        if len(raw) > MAX_VALUES_PER_SAMPLE:
            raise over_limit("values per column", MAX_VALUES_PER_SAMPLE, len(raw), label)
        parsed.append(
            PlotColumn(
                label, tuple(number(v, f"column {label!r}, index {i}") for i, v in enumerate(raw))
            )
        )
    lengths = {len(c.values) for c in parsed}
    if len(lengths) > 1:
        detail = ", ".join(f"{c.label!r} has {len(c.values)}" for c in parsed)
        raise PlotTableError(PlotReason.RAGGED, f"columns differ in length: {detail}")
    return PlotTable(tuple(parsed), _row_labels(labels, len(parsed[0].values)))


def parse_plot_matrix(rows: object, labels: object = None, columns: object = None) -> PlotTable:
    """`[[...], [...]]`: rows of equal length. `labels` name the rows, `columns` the columns
    (default `c0, c1, ...`)."""
    if not isinstance(rows, Sequence) or isinstance(rows, str) or not rows:
        raise PlotTableError(PlotReason.EMPTY, "matrix holds no rows")
    for r, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, str):
            raise PlotTableError(PlotReason.NOT_FINITE, f"matrix row {r} must be a list of numbers")
    width = len(rows[0])
    if width == 0:
        raise PlotTableError(PlotReason.EMPTY, "matrix row 0 is empty")
    for r, row in enumerate(rows):
        if len(row) != width:
            raise PlotTableError(
                PlotReason.RAGGED, f"matrix row {r} has {len(row)} values, row 0 has {width}"
            )
    check_column_count(width)
    if len(rows) > MAX_VALUES_PER_SAMPLE:
        raise over_limit("values per column", MAX_VALUES_PER_SAMPLE, len(rows), None)
    names = _column_names(columns, width)
    parsed: list[PlotColumn] = []
    for c, name in enumerate(names):
        check_label(name, f"column {c}", [p.label for p in parsed])
        values = tuple(number(row[c], f"column {name!r}, row {r}") for r, row in enumerate(rows))
        parsed.append(PlotColumn(name, values))
    return PlotTable(tuple(parsed), _row_labels(labels, len(rows)))


def _column_names(columns: object, width: int) -> list[str]:
    if columns is None:
        return [f"{MATRIX_COLUMN_PREFIX}{c}" for c in range(width)]
    names = _string_list(columns, "columns")
    if len(names) != width:
        raise PlotTableError(
            PlotReason.RAGGED, f"columns has {len(names)} labels for {width} matrix columns"
        )
    return names


def _row_labels(labels: object, row_count: int) -> tuple[str, ...] | None:
    if labels is None:
        return None
    names = _string_list(labels, "labels")
    if len(names) != row_count:
        raise PlotTableError(
            PlotReason.RAGGED, f"labels has {len(names)} entries for {row_count} rows"
        )
    seen: list[str] = []
    for i, name in enumerate(names):
        check_label(name, f"labels[{i}]", seen)
        seen.append(name)
    return tuple(names)


def _string_list(raw: object, what: str) -> list[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise PlotTableError(PlotReason.MISSING_LABEL, f"{what} must be a list of strings")
    for i, name in enumerate(raw):
        if not isinstance(name, str):
            raise PlotTableError(PlotReason.MISSING_LABEL, f"{what}[{i}] must be a string")
    return list(raw)


def check_label(label: str, where: str, taken: Sequence[str]) -> None:
    if not label.strip():
        raise PlotTableError(PlotReason.MISSING_LABEL, f"{where} has a blank label")
    if len(label) > MAX_LABEL_CHARS:
        raise over_limit("label length", MAX_LABEL_CHARS, len(label), label[:MAX_LABEL_CHARS])
    if label in taken:
        raise PlotTableError(PlotReason.DUPLICATE_LABEL, f"label {label!r} is used twice ({where})")


def check_column_count(count: int) -> None:
    if count > MAX_SERIES_PER_PANEL:
        raise over_limit("series (columns) per panel", MAX_SERIES_PER_PANEL, count, None)


def number(raw: object, where: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        raise PlotTableError(
            PlotReason.NOT_FINITE,
            f"{where}: {raw!r} is not a finite number (use null for a missing value)",
        )
    return float(raw)


def over_limit(limit: str, maximum: int, received: int, column: str | None) -> PlotTableError:
    target = f" in column {column!r}" if column is not None else ""
    return PlotTableError(
        PlotReason.OVER_LIMIT, f"{limit}: limit is {maximum}, received {received}{target}"
    )


def check_kind(kind: str) -> None:
    """Refuse a pie-like `kind` (never mapped to bar: the caller re-issues with `kind=bar`) and any
    kind that cannot be drawn yet. Keys on `kind` only; the question text is not inspected."""
    if kind.strip().lower() in PIE_LIKE_KINDS:
        raise PlotTableError(PlotReason.REFUSED_FORM, PIE_REFUSAL)
    if kind not in DRAWABLE_KINDS:
        raise PlotTableError(
            PlotReason.UNKNOWN_KIND, f"kind {kind!r} is not drawable; supported: {DRAWABLE_KINDS}"
        )
