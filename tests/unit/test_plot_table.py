"""PlotTable parsers: the shapes of spec 2.2, the reasons of 2.4 and the limits of 2.3."""

from __future__ import annotations

import pytest

from telemetry_nerd.core.plot_table import (
    MAX_LABEL_CHARS,
    MAX_SERIES_PER_PANEL,
    MAX_VALUES_PER_SAMPLE,
    PlotReason,
    PlotTable,
    PlotTableError,
    parse_plot_array,
    parse_plot_columns,
    parse_plot_matrix,
)


def refused(reason: PlotReason, named: str, build) -> None:
    with pytest.raises(PlotTableError) as e:
        build()
    assert e.value.reason is reason
    assert named in str(e.value)


def test_array_is_one_anonymous_column():
    table = parse_plot_array([1, 2.5, None])
    assert table == PlotTable((parse_plot_columns({"values": [1, 2.5, None]}).columns[0],), None)
    assert table.columns[0].values == (1.0, 2.5, None)


def test_labelled_columns_keep_their_keys_and_row_labels():
    table = parse_plot_columns({"a": [1, 2], "b": [3, 4]}, labels=["x", "y"])
    assert [c.label for c in table.columns] == ["a", "b"]
    assert table.row_labels == ("x", "y")
    assert table.row_count == 2


def test_matrix_is_read_by_column_with_default_names():
    table = parse_plot_matrix([[1, 2, 3], [4, 5, 6]], labels=["r0", "r1"])
    assert [c.label for c in table.columns] == ["c0", "c1", "c2"]
    assert table.columns[1].values == (2.0, 5.0)
    assert table.row_labels == ("r0", "r1")


def test_matrix_uses_supplied_column_names():
    table = parse_plot_matrix([[1, 2]], columns=["p", "q"])
    assert [c.label for c in table.columns] == ["p", "q"]


def test_empty_reason_names_the_empty_column():
    refused(PlotReason.EMPTY, "'b'", lambda: parse_plot_columns({"a": [1], "b": []}))
    refused(PlotReason.EMPTY, "no columns", lambda: parse_plot_columns({}))
    refused(PlotReason.EMPTY, "no rows", lambda: parse_plot_matrix([]))


def test_ragged_reason_names_the_columns_and_rows():
    refused(PlotReason.RAGGED, "'b' has 1", lambda: parse_plot_columns({"a": [1, 2], "b": [1]}))
    refused(PlotReason.RAGGED, "row 1", lambda: parse_plot_matrix([[1, 2], [3]]))
    refused(PlotReason.RAGGED, "labels", lambda: parse_plot_array([1, 2], labels=["x"]))
    refused(PlotReason.RAGGED, "columns", lambda: parse_plot_matrix([[1, 2]], columns=["p"]))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, "3"])
def test_not_finite_reason_names_the_column_and_index(bad):
    refused(
        PlotReason.NOT_FINITE, "column 'b', index 1", lambda: parse_plot_columns({"b": [1, bad]})
    )


def test_not_finite_reason_names_the_matrix_column_and_row():
    refused(
        PlotReason.NOT_FINITE,
        "column 'q', row 1",
        lambda: parse_plot_matrix([[1, 2], [3, float("nan")]], columns=["p", "q"]),
    )


def test_duplicate_label_reason_names_the_label():
    refused(
        PlotReason.DUPLICATE_LABEL, "'p'", lambda: parse_plot_matrix([[1, 2]], columns=["p", "p"])
    )
    refused(PlotReason.DUPLICATE_LABEL, "'x'", lambda: parse_plot_array([1, 2], labels=["x", "x"]))


def test_missing_label_reason_names_the_blank_label():
    refused(PlotReason.MISSING_LABEL, "column ' '", lambda: parse_plot_columns({" ": [1]}))
    refused(
        PlotReason.MISSING_LABEL, "labels[1]", lambda: parse_plot_array([1, 2], labels=["x", ""])
    )
    refused(
        PlotReason.MISSING_LABEL, "column 1", lambda: parse_plot_matrix([[1, 2]], columns=["p", ""])
    )


def test_over_limit_reason_names_the_limit_and_received_value():
    too_many = {f"s{i}": [1] for i in range(MAX_SERIES_PER_PANEL + 1)}
    refused(
        PlotReason.OVER_LIMIT,
        f"limit is {MAX_SERIES_PER_PANEL}, received 9",
        lambda: parse_plot_columns(too_many),
    )
    refused(PlotReason.OVER_LIMIT, "received 9", lambda: parse_plot_matrix([[0] * 9]))
    long_label = "x" * (MAX_LABEL_CHARS + 1)
    refused(
        PlotReason.OVER_LIMIT,
        f"received {MAX_LABEL_CHARS + 1}",
        lambda: parse_plot_columns({long_label: [1]}),
    )
    refused(
        PlotReason.OVER_LIMIT,
        f"column 'values': limit is {MAX_VALUES_PER_SAMPLE}".replace("column 'values': ", ""),
        lambda: parse_plot_array([0.0] * (MAX_VALUES_PER_SAMPLE + 1)),
    )
