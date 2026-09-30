import pytest
from pydantic import TypeAdapter, ValidationError

from telemetry_nerd.workspace.models import AnnotationIn, EvidenceRef, FindingIn, GapIn, Scope

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
