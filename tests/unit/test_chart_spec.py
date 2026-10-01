# tests/unit/test_chart_spec.py
import pytest
from pydantic import ValidationError

from telemetry_nerd.charts.spec import ChartSpec, Layer, auto_spec, validate
from telemetry_nerd.charts.units import infer_unit, unit_from_metric_name


def test_auto_spec_is_line_envelope_scaled_to_data():
    spec = auto_spec("d3")
    assert spec.layers == [Layer(mark="line+envelope", data="d3")]
    assert spec.y.range_mode == "data"
    assert spec.y.unit is None
    assert spec.y.unit_provenance is None


def test_spec_requires_a_layer():
    with pytest.raises(ValidationError):
        ChartSpec(layers=[])


def test_unknown_mark_rejected():
    with pytest.raises(ValidationError):
        Layer(mark="pie", data="d1")


def test_series_budget_error_above_five():
    issues = validate(auto_spec("d1"), {"d1": 6})
    errors = [i for i in issues if i.severity == "error"]
    assert [i.rule for i in errors] == ["series_budget"]
    assert "aggregate" in errors[0].message


def test_five_series_ok_but_unknown_unit_warns():
    issues = validate(auto_spec("d1"), {"d1": 5})
    assert [(i.rule, i.severity) for i in issues] == [("units", "warning")]


def test_known_unit_no_issues():
    spec = auto_spec("d1")
    spec.y.unit = "s"
    assert validate(spec, {"d1": 2}) == []


def test_budget_is_per_chart_across_datasets():
    spec = ChartSpec(
        layers=[Layer(mark="line+envelope", data="a"), Layer(mark="line+envelope", data="b")]
    )
    issues = validate(spec, {"a": 3, "b": 3})
    assert [i.rule for i in issues if i.severity == "error"] == ["series_budget"]


def test_same_dataset_in_two_layers_counted_once():
    spec = ChartSpec(
        layers=[Layer(mark="line+envelope", data="a"), Layer(mark="line+envelope", data="a")]
    )
    assert [i for i in validate(spec, {"a": 3}) if i.severity == "error"] == []


def test_unknown_dataset_is_an_error():
    issues = validate(auto_spec("nope"), {})
    errors = [i for i in issues if i.severity == "error"]
    assert [i.rule for i in errors] == ["unknown_dataset"]
    assert "nope" in errors[0].message


def test_unit_from_metric_name_suffixes():
    assert unit_from_metric_name("tn_demo_latency_seconds") == "s"
    assert unit_from_metric_name("node_memory_MemAvailable_bytes") == "B"
    assert unit_from_metric_name("error_rate_ratio") == "ratio"
    assert unit_from_metric_name("cpu_usage_percent") == "%"
    assert unit_from_metric_name("http_requests_total") == "count"
    assert unit_from_metric_name("histogram_query_seconds_total") == "s"
    assert unit_from_metric_name("tn_demo_latency_seconds_sum") == "s"
    assert unit_from_metric_name("tn_demo_latency_seconds_bucket") == "s"
    assert unit_from_metric_name("tn_demo_latency_seconds_count") == "count"
    assert unit_from_metric_name("up") is None
    assert unit_from_metric_name("queue_depth") is None


def test_infer_unit_from_expr():
    assert infer_unit("rate(tn_demo_latency_seconds_sum[5m])") == "s"
    assert infer_unit('sum by (job) (http_requests_total{code="200"})') == "count"
    assert infer_unit("node_memory_MemAvailable_bytes") == "B"
    assert infer_unit("up") is None
    assert infer_unit("queue_depth") is None


def test_infer_unit_conflicting_suffixes_is_unknown():
    # two metrics with different units: don't guess
    assert infer_unit("x_bytes / y_seconds") is None


def test_auto_spec_infers_unit_with_provenance():
    spec = auto_spec("d1", expr="rate(tn_demo_latency_seconds[5m])")
    assert spec.y.unit == "s"
    assert spec.y.unit_provenance == "inferred from metric name"


def test_auto_spec_without_expr_stays_unitless():
    spec = auto_spec("d1")
    assert spec.y.unit is None
    assert spec.y.unit_provenance is None


def test_auto_spec_explicit_unit_overrides_inference():
    spec = auto_spec("d1", expr="latency_seconds", unit="ms")
    assert spec.y.unit == "ms"
    assert spec.y.unit_provenance is None


def test_inferred_unit_silences_units_warning():
    spec = auto_spec("d1", expr="http_requests_total")
    assert validate(spec, {"d1": 2}) == []


def test_infer_unit_rate_transforms_counters():
    # acceptance (b0v): rate of a counter is per second
    assert infer_unit("rate(tn_calls_total[5m])") == "count/s"
    assert infer_unit("irate(tn_calls_total[5m])") == "count/s"
    assert infer_unit("deriv(tn_calls_total[5m])") == "count/s"
    assert infer_unit("rate(tn_latency_seconds_total[5m])") == "s/s"
    assert infer_unit("deriv(tn_latency_seconds_total[5m])") == "s/s"
    assert infer_unit("rate(tn_bytes_total[5m])") == "B/s"


def test_infer_unit_wraps_aggregations_and_groups():
    # the Grafana Play case that found the bug: aggregators/grouping pass units through
    assert (
        infer_unit("sum by (cloud_region)(rate(traces_spanmetrics_calls_total[5m]))") == "count/s"
    )
    # nested grouping parens must not swallow the transform
    assert infer_unit("sum(rate(tn_calls_total[5m])) / sum(rate(tn_total[5m]))") == "count/s"


def test_infer_unit_increase_keeps_count():
    assert infer_unit("increase(tn_calls_total[1h])") == "count"
    assert infer_unit("delta(tn_latency_seconds_total[1h])") == "s"


def test_infer_unit_rate_of_gauge_unchanged():
    # rate/increase semantics only transform counters; gauges keep their unit
    assert infer_unit("rate(tn_demo_latency_seconds_sum[5m])") == "s"
    assert infer_unit("rate(tn_demo_latency_seconds[5m])") == "s"


def test_infer_unit_histogram_quantile_base_unit():
    assert (
        infer_unit("histogram_quantile(0.9, sum by (le) (rate(tn_latency_seconds_bucket[5m])))")
        == "s"
    )


def test_infer_unit_rate_mismatch_with_plain_metric_is_unknown():
    assert infer_unit("rate(tn_calls_total[5m]) / tn_calls_total") is None


def test_auto_spec_draws_distributions_as_heatmaps():
    assert auto_spec("d1", representation="distribution").layers[0].mark == "heatmap"


def test_marks_must_match_the_representation():
    lines = validate(auto_spec("d1"), {"d1": 1}, {"d1": "distribution"})
    assert "mark_representation" in [i.rule for i in lines if i.severity == "error"]
    heat = validate(auto_spec("d1", representation="distribution"), {"d1": 1}, {"d1": "bucket_agg"})
    assert "mark_representation" in [i.rule for i in heat if i.severity == "error"]


def test_heatmap_small_multiple_budget():
    spec = auto_spec("d1", representation="distribution")
    assert [
        i.rule for i in validate(spec, {"d1": 12}, {"d1": "distribution"}) if i.severity == "error"
    ] == []
    assert "series_budget" in [i.rule for i in validate(spec, {"d1": 13}, {"d1": "distribution"})]


def test_histogram_needs_windows():
    spec = ChartSpec(layers=[Layer(mark="histogram", data="d1")])
    assert "windows" in [i.rule for i in validate(spec, {"d1": 1}, {"d1": "distribution"})]
