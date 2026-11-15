"""parse_plot_csv: the CSV rules of spec 2.2 and the refusals of 2.4."""

from __future__ import annotations

import pytest

from telemetry_nerd.core.plot_csv import csv_inline_series, parse_plot_csv
from telemetry_nerd.core.plot_table import (
    MAX_CSV_BYTES,
    PlotReason,
    PlotTableError,
)

NOW = 1_700_000_000_000


def test_header_time_label_and_numeric_columns_are_told_apart():
    table = parse_plot_csv("Time,host,a,b\n1000,x,1,\n2000,y,2,5\n", NOW)
    assert table.time_ms == (1000, 2000)
    assert table.row_labels == ("x", "y")
    assert [(c.label, c.values) for c in table.columns] == [
        ("a", (1.0, 2.0)),
        ("b", (None, 5.0)),
    ]


def test_quoted_cells_follow_rfc_4180():
    table = parse_plot_csv('ts,"a, b"\n1000,1\n', NOW)
    assert table.columns[0].label == "a, b"


def test_empty_cell_is_omitted_from_the_line_so_it_shows_a_gap():
    series = csv_inline_series(parse_plot_csv("ts,a\n1000,1\n2000,\n3000,3\n", NOW))
    assert series[0].ts_ms == (1000, 3000)
    assert series[0].values == (1.0, 3.0)


@pytest.mark.parametrize(
    ("csv", "reason"),
    [
        ("ts;a\n1000;1\n", PlotReason.CSV_UNSUPPORTED_DELIMITER),
        ("ts\ta\n1000\t1\n", PlotReason.CSV_UNSUPPORTED_DELIMITER),
        ("sep=;\nts,a\n1000,1\n", PlotReason.CSV_UNSUPPORTED_DELIMITER),
        ("ts,a\n1000,1\n2000\n", PlotReason.RAGGED),
        ("ts,a\n1000,NaN\n", PlotReason.NOT_FINITE),
        ("ts,a\n2000,1\n1000,2\n", PlotReason.TS_NOT_INCREASING),
        ("ts,a\nyesterday,1\n", PlotReason.TS_UNPARSEABLE),
        ("ts,a\n1000,\n", PlotReason.EMPTY),
        ("ts,a\n", PlotReason.EMPTY),
        ("", PlotReason.EMPTY),
        ("ts,a,a\n1000,1,2\n", PlotReason.DUPLICATE_LABEL),
        ("ts,h,k,a\n1000,x,y,1\n", PlotReason.CSV_MALFORMED),
        ('ts,a\n1000,"1\n', PlotReason.CSV_MALFORMED),
    ],
)
def test_bad_csv_is_refused_with_its_reason(csv, reason):
    with pytest.raises(PlotTableError) as e:
        parse_plot_csv(csv, NOW)
    assert e.value.reason is reason


def test_csv_over_5_mib_is_refused_naming_the_limit():
    with pytest.raises(PlotTableError) as e:
        parse_plot_csv("ts,a\n1000,1\n" + "x" * MAX_CSV_BYTES, NOW)
    assert e.value.reason is PlotReason.OVER_LIMIT
    assert str(MAX_CSV_BYTES) in str(e.value)


def test_a_line_without_a_time_column_is_refused():
    with pytest.raises(PlotTableError) as e:
        csv_inline_series(parse_plot_csv("a,b\n1,2\n", NOW))
    assert e.value.reason is PlotReason.KIND_SHAPE_MISMATCH
