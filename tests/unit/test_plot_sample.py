"""Windowless sample dataset: caller columns drawn by window marks with no time axis."""

from __future__ import annotations

import pytest

from telemetry_nerd.charts.spec import ChartSpec, Layer, validate
from telemetry_nerd.core.plot_table import (
    PlotReason,
    PlotTableError,
    parse_plot_array,
    parse_plot_columns,
)
from telemetry_nerd.core.service import ChartRejected

from .fakes import make_service

LATENCIES = [float(i % 50) for i in range(1000)]


def test_histogram_of_a_supplied_array_states_n_and_the_bin_rule(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.plot_sample("How are latencies spread?", parse_plot_array(LATENCIES), unit="ms")
    panel = svc.workspace.get_panel(shown.panel.id)
    layer = panel.spec["layers"][0]
    assert layer["mark"] == "histogram" and layer["columns"] == ["values"] and not layer["windows"]
    assert validate(ChartSpec.model_validate(panel.spec), {panel.dataset_ids[0]: 1},
                    {panel.dataset_ids[0]: "samples"}) == []  # fmt: skip

    data = svc.panel_data(panel.id, width_px=800)
    assert data["series"][0]["n"] == 1000 and data["series"][0]["missing"] == 0
    assert sum(data["series"][0]["counts"]) == 1000
    assert "n=1000" in data["text"] and "Freedman-Diaconis" in data["text"]
    assert len(data["edges"]) == len(data["series"][0]["counts"]) + 1


def test_compared_columns_share_bins_and_count_nulls_as_missing(tmp_path):
    svc = make_service(tmp_path)
    table = parse_plot_columns({"before": [1, 2, 3, None], "after": [2, 3, 4, 5]})
    shown = svc.plot_sample("Did it shift?", table)
    data = svc.panel_data(shown.panel.id, width_px=800)
    before, after = data["series"]
    assert (before["label"], before["n"], before["missing"]) == ("before", 3, 1)
    assert (after["label"], after["n"], after["missing"]) == ("after", 4, 0)
    assert len(before["counts"]) == len(after["counts"]) == len(data["edges"]) - 1
    assert "before: n=3, missing=1" in data["text"]


def test_a_mark_that_needs_time_windows_cannot_draw_a_sample(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ChartRejected, match="no time axis"):
        svc.plot_sample("q?", parse_plot_array([1.0, 2.0]), mark="percentiles")
    assert svc.workspace.list_panels() == []


def test_a_time_window_on_a_sample_layer_is_rejected():
    spec = ChartSpec(layers=[Layer(mark="histogram", data="d1", columns=["a"],
                                   windows=[{"start_ms": 0, "end_ms": 1}])])  # fmt: skip
    issues = validate(spec, {"d1": 1}, {"d1": "samples"})
    assert "windows" in [i.rule for i in issues]
    assert any("no time axis" in i.message for i in issues)


async def test_a_sample_panel_refuses_rescope_as_fixed_data(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.plot_sample("q?", parse_plot_array(LATENCIES))
    with pytest.raises(ValueError, match="rescope_unsupported_for_mark"):
        await svc.rescope(shown.panel.id, "now-1h", "now")
    meta = svc.datasets.meta(svc.workspace.get_panel(shown.panel.id).dataset_ids[0])
    from telemetry_nerd.core.code_outputs import refuse_requery

    with pytest.raises(ValueError, match="fixed data, not a source query"):
        refuse_requery(meta, "a time-range rescope")


def test_a_10x10_matrix_draws_a_heatmap_with_both_label_sets_and_the_colour_mapping(tmp_path):
    svc = make_service(tmp_path)
    matrix = [[float(10 * r + c) for c in range(10)] for r in range(10)]
    rows = [f"row{r}" for r in range(10)]
    cols = [f"col{c}" for c in range(10)]
    shown = svc.plot_heatmap("Which pairs correlate?", matrix, columns=cols, labels=rows)
    panel = svc.workspace.get_panel(shown.panel.id)
    assert panel.spec["layers"][0]["mark"] == "heatmap"

    data = svc.panel_data(panel.id, width_px=800)
    assert data["row_labels"] == rows and data["column_labels"] == cols
    assert data["cells"][3][4] == 34.0
    assert (data["min"], data["max"]) == (0.0, 99.0)
    assert "linear from 0 (darkest) to 99 (lightest)" in data["legend"]


def test_heatmap_rows_and_columns_default_to_numbered_labels_and_nulls_are_blank(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.plot_heatmap("q?", [[1, None], [3, 4]])
    data = svc.panel_data(shown.panel.id, width_px=800)
    assert data["row_labels"] == ["r0", "r1"] and data["column_labels"] == ["c0", "c1"]
    assert data["cells"] == [[1.0, None], [3.0, 4.0]]
    assert "1 missing cells" in data["legend"]


def test_a_heatmap_wider_or_taller_than_100_is_refused(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(PlotTableError, match="limit is 100, received 101") as wide:
        svc.plot_heatmap("q?", [[0.0] * 101])
    assert wide.value.reason is PlotReason.OVER_LIMIT
    with pytest.raises(PlotTableError, match="matrix rows: limit is 100, received 101"):
        svc.plot_heatmap("q?", [[0.0]] * 101)
    assert svc.workspace.list_panels() == []


def test_qq_of_two_columns_of_unequal_length_states_both_n(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.plot_cumulative(
        "Did the shift move the tail?", {"before": [1, 2, 3, 4, 5, 6], "after": [2, 3, None, 5]},
        kind="qq",
    )  # fmt: skip
    panel = svc.workspace.get_panel(shown.panel.id)
    assert panel.spec["layers"][0]["mark"] == "qq"
    data = svc.panel_data(panel.id, width_px=800)
    before, after = data["series"][0]["windows"]
    assert (before["label"], before["n"], before["missing"]) == ("before", 6.0, 0)
    assert (after["label"], after["n"], after["missing"]) == ("after", 3.0, 1)
    assert after["lo"] == after["hi"] == [2.0, 3.0, 5.0] and after["c"] == [1, 1, 1]


@pytest.mark.parametrize("columns", [{"a": [1.0, 2.0]}, {"a": [1.0], "b": [2.0], "c": [3.0]}])
def test_qq_needs_exactly_two_columns(tmp_path, columns):
    svc = make_service(tmp_path)
    with pytest.raises(PlotTableError) as refused:
        svc.plot_cumulative("q?", columns, kind="qq")
    assert refused.value.reason == PlotReason.KIND_SHAPE_MISMATCH
    assert svc.workspace.list_panels() == []


@pytest.mark.parametrize("kind", ["quantile_curve", "ccdf"])
def test_tail_views_draw_an_array_and_refuse_five_columns(tmp_path, kind):
    svc = make_service(tmp_path)
    shown = svc.plot_cumulative("How heavy is the tail?", LATENCIES, kind=kind, unit="ms")
    data = svc.panel_data(shown.panel.id, width_px=800)
    (window,) = data["series"][0]["windows"]
    assert data["mark"] == kind and window["n"] == 1000.0 and sum(window["c"]) == 1000

    five = {f"c{i}": [1.0, 2.0] for i in range(5)}
    with pytest.raises(PlotTableError) as refused:
        svc.plot_cumulative("q?", five, kind=kind)
    assert refused.value.reason == PlotReason.OVER_LIMIT


def test_a_qq_sample_with_three_columns_fails_spec_validation():
    spec = ChartSpec(layers=[Layer(mark="qq", data="d1", columns=["a", "b", "c"])])
    issues = validate(spec, {"d1": 3}, {"d1": "samples"})
    assert any("exactly 2" in i.message for i in issues)
