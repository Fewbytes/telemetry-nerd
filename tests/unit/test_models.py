import pytest
from pydantic import TypeAdapter, ValidationError

from telemetry_nerd.workspace.models import (
    AnnotationIn,
    EvidenceRef,
    FindingIn,
    GapIn,
    Scope,
    StatisticRef,
)

SCOPE = {
    "source": "default",
    "selector": 'checkout_latency{service="checkout"}',
    "time_range": {"start_ms": 1_000, "end_ms": 61_000},
    "step": "30s",
    "aggregation": "avg",
}
ref = TypeAdapter(EvidenceRef)


def test_scope_requires_all_fields():
    Scope(**SCOPE)
    for key in ("source", "selector", "time_range", "step", "aggregation"):
        with pytest.raises(ValidationError):
            Scope(**{k: v for k, v in SCOPE.items() if k != key})


def test_scope_rejects_bad_step_and_empty_range():
    with pytest.raises(ValidationError, match="duration"):
        Scope(**{**SCOPE, "step": "soon"})
    with pytest.raises(ValidationError):
        Scope(**{**SCOPE, "time_range": {"start_ms": 5, "end_ms": 5}})


def test_scope_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        Scope(**SCOPE, region="eu")


def test_statistic_requires_interval_unless_exact():
    with pytest.raises(ValidationError, match="no_uncertainty"):
        ref.validate_python(
            {
                "kind": "statistic",
                "dataset": "d1",
                "name": "p99 ratio",
                "value": 2.1,
                "method": "bootstrap",
            }
        )
    ref.validate_python(
        {
            "kind": "statistic",
            "dataset": "d1",
            "name": "ratio",
            "value": 2.1,
            "interval": [1.8, 2.4],
            "method": "bootstrap",
        }
    )
    ref.validate_python(
        {
            "kind": "statistic",
            "dataset": "d1",
            "name": "errors",
            "value": 42,
            "exact": True,
            "method": "count",
        }
    )


def test_statistic_interval_ordered():
    with pytest.raises(ValidationError):
        ref.validate_python(
            {
                "kind": "statistic",
                "dataset": "d1",
                "name": "x",
                "value": 1,
                "interval": [2, 1],
                "method": "m",
            }
        )


def test_finding_requires_evidence_and_consistent_stance():
    base = {"claim": "p99 doubled", "scope": SCOPE}
    with pytest.raises(ValidationError):
        FindingIn(**base, evidence=[])
    ev = [{"kind": "panel", "panel": "p1"}]
    FindingIn(**base, evidence=ev)
    with pytest.raises(ValidationError, match="stance"):
        FindingIn(**base, evidence=ev, hypothesis="h1")
    FindingIn(**base, evidence=ev, hypothesis="h1", stance="for")


@pytest.mark.parametrize(
    "data,ok",
    [
        ({"kind": "event", "t_start_ms": 5}, True),
        ({"kind": "event"}, False),
        ({"kind": "region", "t_start_ms": 5, "t_end_ms": 9}, True),
        ({"kind": "region", "t_start_ms": 9, "t_end_ms": 5}, False),
        ({"kind": "threshold", "value": 0.5}, True),
        ({"kind": "threshold"}, False),
        ({"kind": "band", "value": 1, "value_hi": 2}, True),
        ({"kind": "band", "value": 2, "value_hi": 1}, False),
        ({"kind": "note", "panel": "p1", "label": "odd dip"}, True),
        ({"kind": "note", "label": "odd dip"}, False),
        ({"kind": "note", "panel": "p1"}, False),
    ],
)
def test_annotation_kind_rules(data, ok):
    if ok:
        AnnotationIn(**data)
    else:
        with pytest.raises(ValidationError):
            AnnotationIn(**data)


def test_gap_suggestion_type():
    GapIn(
        missing_signal="in-flight requests",
        needed_for="littles_law",
        suggestion={"name": "http_server_active_requests", "type": "gauge", "labels": ["service"]},
    )
    with pytest.raises(ValidationError):
        GapIn(missing_signal="x", needed_for="y", suggestion={"name": "m", "type": "blob"})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_rejected(bad):
    stat = {"kind": "statistic", "dataset": "d", "name": "x", "method": "m"}
    with pytest.raises(ValidationError):
        ref.validate_python({**stat, "value": bad, "interval": [0, 1]})
    with pytest.raises(ValidationError):
        ref.validate_python({**stat, "value": 1, "interval": [0, bad]})
    with pytest.raises(ValidationError):
        ref.validate_python({**stat, "value": 1, "interval": [bad, 2]})
    with pytest.raises(ValidationError):
        AnnotationIn(kind="threshold", value=bad)
    with pytest.raises(ValidationError):
        AnnotationIn(kind="band", value=1, value_hi=bad)


def test_exact_rules():
    stat = {"kind": "statistic", "dataset": "d", "name": "x", "method": "m"}
    with pytest.raises(ValidationError, match="contradictory"):
        ref.validate_python({**stat, "value": 3, "exact": True, "interval": [3, 3]})
    with pytest.raises(ValidationError, match="integral"):
        ref.validate_python({**stat, "value": 2.5, "exact": True})
    ref.validate_python({**stat, "value": 3.0, "exact": True})


def test_inputs_reject_server_fields():
    with pytest.raises(ValidationError):
        AnnotationIn(kind="event", t_start_ms=1, id="a9")
    with pytest.raises(ValidationError):
        AnnotationIn(kind="event", t_start_ms=1, deleted=True)
    with pytest.raises(ValidationError):
        FindingIn(claim="c", scope=SCOPE, evidence=[{"kind": "panel", "panel": "p"}], id="f9")
    with pytest.raises(ValidationError):
        FindingIn(claim="c", scope=SCOPE, evidence=[{"kind": "panel", "panel": "p"}], deleted=True)


def test_unknown_evidence_kind_rejected():
    with pytest.raises(ValidationError):
        ref.validate_python({"kind": "vibes", "panel": "p1"})


def _stat(**kw):
    base = {
        "kind": "statistic",
        "dataset": "d1",
        "name": "p95",
        "value": 0.7,
        "interval": [0.6, 0.8],
        "method": "histogram_quantile",
    }
    return StatisticRef.model_validate(base | kw)


def test_percentile_statistic_requires_n():
    with pytest.raises(ValidationError, match="percentile_without_n"):
        _stat()


def test_percentile_statistic_requires_meaningful_n():
    with pytest.raises(ValidationError, match="percentile_not_meaningful"):
        _stat(params={"n": 13})
    assert _stat(params={"n": 2328}).params["n"] == 2328


def test_percentile_q_from_name_or_params():
    with pytest.raises(ValidationError, match="percentile_not_meaningful"):
        _stat(name="p99.9", params={"n": 2000})  # needs 10000
    with pytest.raises(ValidationError, match="params.q"):
        _stat(name="quantile", params={"n": 5000})
    assert _stat(name="quantile", params={"n": 5000, "q": 0.99})


def test_non_percentile_statistics_unaffected():
    assert _stat(name="mean_latency", params={})


@pytest.mark.parametrize(
    "name",
    ["p95_latency", "latency_p99", "median_age", "95th percentile", "q99", "pct99", "quantiles"],
)
def test_percentile_names_are_recognised_anywhere(name):
    with pytest.raises(ValidationError, match="percentile_without_n|params.q"):
        _stat(name=name)


def test_percentile_n_may_be_an_integral_float_and_mad_is_not_a_percentile():
    assert _stat(params={"n": 5000.0})
    assert _stat(name="median absolute deviation", params={})
