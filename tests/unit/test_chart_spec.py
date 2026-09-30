# tests/unit/test_chart_spec.py
import pytest
from pydantic import ValidationError

from telemetry_nerd.charts.spec import ChartSpec, Layer, auto_spec, validate


def test_auto_spec_is_line_envelope_scaled_to_data():
    spec = auto_spec("d3")
    assert spec.layers == [Layer(mark="line+envelope", data="d3")]
    assert spec.y.range_mode == "data"


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
