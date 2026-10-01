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
