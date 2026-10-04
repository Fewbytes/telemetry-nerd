"""The lesson scope guard (spec 2026-10-04 R5): a lesson is never broader than its evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from telemetry_nerd.retro.guard import check, covers, evidence_from_exprs, in_family, overlaps
from telemetry_nerd.retro.models import LessonScope


def ev(expr: str | None, source: str = "prom", obj: str = "f1"):
    return evidence_from_exprs(obj, source, [expr])


def scope(**kw) -> LessonScope:
    return LessonScope.model_validate({"source": "prom", **kw})


def test_service_evidence_covers_a_service_lesson_not_a_source_wide_one():
    e = ev('rate(http_requests_total{service_name="checkout"}[5m])')
    assert covers(scope(service="checkout"), e) is None
    why = covers(scope(), e)
    assert why is not None and "not the whole source" in why
    assert "not service cart" in (covers(scope(service="cart"), e) or "")


def test_source_wide_evidence_covers_narrower_lessons():
    e = ev("sum by (service_name) (rate(http_requests_total[5m]))")
    assert covers(scope(), e) is None
    assert covers(scope(service="checkout"), e) is None
    assert covers(scope(labels={"namespace": "prod"}), e) is None


def test_service_labels_are_aliases_of_one_identity():
    e = ev('x{service_name="checkout", job="otel/checkout"}')
    assert covers(scope(service="checkout"), e) is None


def test_aggregated_away_service_still_pins_the_evidence():
    e = ev('sum by (code) (rate(x{service_name="payment"}[1m]))')
    assert covers(scope(), e) is not None
    assert covers(scope(service="payment"), e) is None


def test_regex_on_service_matches_named_service_only():
    e = ev('x{service_name=~"checkout|cart"}')
    assert covers(scope(service="cart"), e) is None
    assert covers(scope(service="payment"), e) is not None
    assert covers(scope(), e) is not None


def test_keep_all_matchers_do_not_restrict_and_signal_labels_never_do():
    assert covers(scope(), ev('x{service_name!="", code=~"5..", le="0.5"}')) is None
    assert covers(scope(), ev('x{service_name=~".+"}')) is None


def test_other_entity_labels_must_be_pinned_by_the_lesson():
    e = ev('x{namespace="prod"}')
    assert "add namespace" in (covers(scope(), e) or "")
    assert covers(scope(labels={"namespace": "prod"}), e) is None
    assert covers(scope(labels={"namespace": "dev"}), e) is not None


def test_source_must_match():
    assert "source 'other'" in (covers(scope(), ev("x", source="other")) or "")


def test_metric_family_must_contain_an_evidence_metric():
    e = ev('rate(http_server_duration_seconds_bucket{service_name="a"}[5m])')
    assert covers(scope(service="a", metric_family="http_server_"), e) is None
    assert covers(scope(service="a", metric_family="http_server_*_bucket"), e) is None
    assert covers(scope(service="a", metric_family="rpc_"), e) is not None
    assert in_family("rpc_calls_total", "rpc_*") and not in_family("x_rpc", "rpc_")


def test_unreadable_evidence_covers_nothing():
    e = evidence_from_exprs("p1", "prom", [None])
    assert e.alternatives is None
    assert "undetermined" in (covers(scope(), e) or "")


def test_check_needs_one_item_covering_all_and_lists_partial():
    items = [
        ev('x{service_name="checkout"}', obj="f1"),
        ev('x{service_name="cart"}', obj="f2"),
    ]
    with pytest.raises(ValueError, match="lesson_beyond_evidence"):
        check(scope(), items)  # two services observed is not the source
    sc = check(scope(service="checkout"), items)
    assert sc.covered_by == ["f1"] and [p["id"] for p in sc.partial] == ["f2"]


def test_either_alternative_of_an_or_covers():
    e = ev('x{service_name="a"} or x{service_name="b"}')
    assert covers(scope(service="b"), e) is None


def test_overlap_for_refutation():
    lesson = scope(service="checkout")
    assert overlaps(lesson, ev('x{service_name="checkout", pod="p1"}')) is None
    assert overlaps(lesson, ev("x")) is None  # source-wide evidence includes checkout
    assert overlaps(lesson, ev('x{service_name="cart"}')) is not None
    assert overlaps(lesson, ev("x", source="other")) is not None


def test_scope_model_refuses_service_labels_and_non_entity_labels():
    with pytest.raises(ValidationError, match="scope.service"):
        scope(labels={"service_name": "x"})
    with pytest.raises(ValidationError, match="not an entity label"):
        scope(labels={"status_code": "500"})
    with pytest.raises(ValidationError):
        LessonScope.model_validate({"service": "x"})  # source is required
    assert scope(service="a", labels={"namespace": "p"}).describe() == (
        'source prom, service a, namespace="p"'
    )
