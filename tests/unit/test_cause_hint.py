"""finding_create's gentle cause-hypothesis hint (bead t75)."""

from __future__ import annotations

from telemetry_nerd.core.cause_hint import cause_hint, subjects
from telemetry_nerd.workspace.models import Finding, Hypothesis, ScopeCheck


def finding(source="special_cause", named=('service_name="payment"',), hyp=None, stance=None):
    return Finding.model_validate(
        {
            "id": "f1",
            "author": "claude",
            "created_at_ms": 0,
            "claim": "payment charge errors rose at 10:07",
            "scope": {
                "source": "default",
                "selector": 'traces_span_metrics_calls_total{service_name="payment"}',
                "time_range": {"start_ms": 0, "end_ms": 60_000},
                "step": "30s",
                "aggregation": "sum by (service_name) (rate(...[1m]))",
            },
            "evidence": [
                {
                    "kind": "statistic",
                    "dataset": "d1",
                    "name": "level_shift",
                    "value": 0.4,
                    "interval": [0.3, 0.5],
                    "method": "analyze",
                    "source": source,
                }
            ],
            "hypothesis": hyp,
            "stance": stance,
            "scope_check": ScopeCheck(status="covered", named=list(named)),
        }
    )


def hyp(statement, status="proposed", id="h1"):
    return Hypothesis(
        id=id, statement=statement, status=status, author="claude", created_at_ms=0,
        updated_at_ms=0,
    )  # fmt: skip


def test_hint_when_no_hypothesis_names_the_subject():
    h = cause_hint(finding(), [hyp("the cart cartFailure flag was switched on", "refuted")])
    assert h is not None and "payment" in h and "competing cause" in h
    assert "hypothesis_create" in h


def test_no_hint_when_a_live_hypothesis_names_it_or_the_finding_backs_one():
    assert cause_hint(finding(), [hyp("Payment charge calls fail (paymentFailure flag)")]) is None
    assert cause_hint(finding(hyp="h1", stance="for"), []) is None
    # a refuted hypothesis naming it does not count: the episode still needs a live cause
    assert cause_hint(finding(), [hyp("payment is overloaded", "refuted")]) is not None


def test_no_hint_for_common_cause_or_without_subject_when_hypotheses_exist():
    assert cause_hint(finding(source="common_cause"), []) is None
    assert cause_hint(finding(named=()), [hyp("arrival surge drives the backlog")]) is None
    assert "the episode" in (cause_hint(finding(named=()), []) or "")


def test_subjects_reads_label_values():
    assert subjects(finding(named=('service_name="payment"', 'pod="p-1"'))) == ["payment", "p-1"]
