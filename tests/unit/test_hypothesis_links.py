"""Hypothesis discipline beyond `supported` (eval round 3, d77.3):

* aiy: one finding may take a stance on several hypotheses (for h1, against h2), and Claude may
  mark a hypothesis refuted only with a finding against it or an explicit reason (inconclusive:
  a linked finding, a reason or a note). The user's own status changes are not checked.
* qy7q: hypothesis_create takes a scope, in a finding scope's shape or as prose (stored as text).

Shapes follow the overload_spike / payment-failure live-sonnet-3 runs."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client
from pydantic import ValidationError

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.models import Finding, FindingIn, HypothesisScope
from telemetry_nerd.workspace.objects import ObjectStore

from .fakes import NOW, make_service
from .test_workspace_service import finding_in

# overload_spike.live-sonnet-3: the two competing causes
H1 = ("checkout: the 14:38-14:41 episode is an arrival surge (request rate ~3x) that pushed the "
      "service into queueing, raising latency and in-flight concurrency")  # fmt: skip
H2 = ("checkout: the 14:38-14:41 episode is the checkout service itself slowing down (latency "
      "shift) at unchanged arrival rate, piling up in-flight requests")  # fmt: skip
# payment-failure.live-sonnet-3: the refused scope argument
PAYMENT_SCOPE = "service_name in payment, checkout, frontend; 2026-10-03 14:30-15:00 UTC"


@pytest.fixture
def svc(tmp_path):
    s = make_service(tmp_path)
    asyncio.run(s.query("up", start="now-1h", end="now"))
    return s


async def call(mcp, name, args):
    async with Client(mcp) as c:
        return await c.call_tool(name, args)


def text(r) -> str:
    return r.content[0].text


# --- aiy: several links per finding -----------------------------------------------------------


def test_one_finding_backs_h1_and_counts_against_h2(svc):
    h1 = svc.ws.hypothesis_create(H1, "claude")
    h2 = svc.ws.hypothesis_create(H2, "claude")
    links = [{"id": h1.id, "stance": "for"}, {"id": h2.id, "stance": "against"}]
    f = svc.ws.finding_create(finding_in(svc, hypotheses=links), "claude")
    assert [(x.id, x.stance) for x in f.hypotheses] == [("h1", "for"), ("h2", "against")]
    assert svc.ws.objects.get_hypothesis("h1").evidence_for == [f.id]
    assert svc.ws.objects.get_hypothesis("h2").evidence_against == [f.id]
    created = svc.log.since(0)[-1]
    assert created.type == "finding.created" and created.payload["hypotheses"] == links
    # h2 may now be refuted (the finding against it), and h1 supported against it
    assert svc.ws.hypothesis_update("h2", "refuted", "claude").status == "refuted"


def test_links_validate():
    base = {"claim": "c", "scope": {"source": "default", "selector": "up", "step": "1m",
                                    "aggregation": "avg",
                                    "time_range": {"start_ms": 0, "end_ms": 1}},
            "evidence": [{"kind": "panel", "panel": "p1"}]}  # fmt: skip
    with pytest.raises(ValidationError, match="both for and against h1"):
        FindingIn.model_validate(base | {"hypotheses": [{"id": "h1", "stance": "for"},
                                                        {"id": "h1", "stance": "against"}]})  # fmt: skip
    with pytest.raises(ValidationError, match="different stances"):
        FindingIn.model_validate(base | {"hypothesis": "h1", "stance": "for",
                                         "hypotheses": [{"id": "h1", "stance": "against"}]})  # fmt: skip
    with pytest.raises(ValidationError, match="together"):
        FindingIn.model_validate(base | {"hypothesis": "h1"})
    both = FindingIn.model_validate(base | {"hypothesis": "h1", "stance": "for",
                                            "hypotheses": [{"id": "h2", "stance": "against"},
                                                           {"id": "h2", "stance": "against"}]})  # fmt: skip
    assert [(x.id, x.stance) for x in both.hypotheses] == [("h1", "for"), ("h2", "against")]


def test_unknown_hypothesis_in_any_link_is_refused_and_nothing_is_written(svc):
    h1 = svc.ws.hypothesis_create(H1, "claude")
    before = svc.log.last_seq
    links = [{"id": h1.id, "stance": "for"}, {"id": "h9", "stance": "against"}]
    with pytest.raises(Exception, match="h9"):
        svc.ws.finding_create(finding_in(svc, hypotheses=links), "claude")
    assert svc.log.last_seq == before and svc.ws.objects.get_hypothesis("h1").evidence_for == []


def test_findings_stored_before_aiy_load_as_one_link(tmp_path):
    """Migration: a stored row with the single hypothesis/stance reads as a one-item list (and
    one without a link as none); rewritten rows carry only the list."""
    con = open_workspace_db(tmp_path / "w.db")
    store = ObjectStore(con, lambda p: f"{p}1")
    old = {"id": "f1", "claim": "c", "author": "claude", "created_at_ms": 1,
           "scope": {"source": "default", "selector": "up", "step": "1m", "aggregation": "avg",
                     "time_range": {"start_ms": 0, "end_ms": 1}},
           "evidence": [{"kind": "panel", "panel": "p1"}], "caveats": [],
           "hypothesis": "h2", "stance": "against", "answers_panel": None}  # fmt: skip
    con.execute(
        "INSERT INTO objects (id, kind, anchor, deleted, created_at_ms, data) "
        "VALUES ('f1', 'finding', NULL, 0, 1, ?)",
        (json.dumps(old),),
    )
    f = store.get_finding("f1")
    assert [(x.id, x.stance) for x in f.hypotheses] == [("h2", "against")]
    rewritten = json.loads(
        store.set_verdict("f1", "accepted", None).model_dump_json()
    )  # the next write stores the list form
    assert "hypothesis" not in rewritten and rewritten["hypotheses"] == [
        {"id": "h2", "stance": "against"}
    ]
    none = Finding.model_validate(old | {"hypothesis": None, "stance": None})
    assert none.hypotheses == []


# --- aiy: refuted / inconclusive need grounds (Claude only) -----------------------------------


def test_the_round3_refutation_without_a_finding_against_is_refused(svc):
    """overload_spike round 3: h2 refuted with a note citing f3, which was linked to h1 only."""
    h1 = svc.ws.hypothesis_create(H1, "claude")
    h2 = svc.ws.hypothesis_create(H2, "claude")
    svc.ws.finding_create(finding_in(svc, hypothesis=h1.id, stance="for"), "claude")
    note = "Rate was not unchanged: it rose ~3x at the peak (f3)."
    with pytest.raises(ValueError, match="cannot mark h2 refuted: h2 has no finding against it"):
        svc.ws.hypothesis_update(h2.id, "refuted", "claude", note=note)
    assert svc.ws.objects.get_hypothesis(h2.id).status == "proposed"
    # an explicit reason is accepted and stored with the status
    h = svc.ws.hypothesis_update(h2.id, "refuted", "claude", note=note, reason="f3: rate rose ~3x")
    assert (h.status, h.status_reason) == ("refuted", "f3: rate rose ~3x")
    assert svc.log.since(0)[-1].payload["reason"] == "f3: rate rose ~3x"
    # moving on clears the reason: it belonged to that status
    assert svc.ws.hypothesis_update(h2.id, "proposed", "claude").status_reason is None


def test_a_rejected_finding_against_does_not_refute(svc):
    h = svc.ws.hypothesis_create(H2, "claude")
    f = svc.ws.finding_create(finding_in(svc, hypothesis=h.id, stance="against"), "claude")
    svc.ws.finding_verdict(f.id, "rejected", "user")
    with pytest.raises(ValueError, match="no finding against it the user has not rejected"):
        svc.ws.hypothesis_update(h.id, "refuted", "claude")


def test_inconclusive_needs_a_link_a_reason_or_a_note(svc):
    h = svc.ws.hypothesis_create(H2, "claude")
    with pytest.raises(ValueError, match="cannot mark h1 inconclusive"):
        svc.ws.hypothesis_update(h.id, "inconclusive", "claude")
    out = svc.ws.hypothesis_update(h.id, "inconclusive", "claude", note="no arrivals counter")
    assert out.status_reason == "no arrivals counter"
    h2 = svc.ws.hypothesis_create(H1, "claude")
    svc.ws.finding_create(finding_in(svc, hypothesis=h2.id, stance="for"), "claude")
    assert svc.ws.hypothesis_update(h2.id, "inconclusive", "claude").status == "inconclusive"


def test_user_status_changes_are_not_checked(svc):
    h = svc.ws.hypothesis_create(H2, "claude")
    assert svc.ws.hypothesis_update(h.id, "refuted", "user").status == "refuted"
    assert svc.ws.hypothesis_update(h.id, "inconclusive", "user").status == "inconclusive"
    # a user note on a ruled-out status is shown as its reason
    assert svc.ws.hypothesis_update(h.id, "refuted", "user", note="saw it").status_reason == (
        "saw it"
    )


async def test_mcp_finding_create_with_hypotheses_and_refuted_reason(tmp_path):
    s = make_service(tmp_path)
    await s.query("up", start="now-1h", end="now")
    mcp = build_mcp(s, "http://x")
    for st in (H1, H2):
        await call(mcp, "hypothesis_create", {"statement": st})
    args = {"claim": "p99 doubled", "evidence": [{"kind": "statistic", "dataset": "d1",
            "name": "n", "value": 1.0, "exact": True, "method": "count"}],
            "scope": {"source": "default", "selector": "up", "start": NOW - 3_600_000,
                      "end": NOW, "step": "1m"}}  # fmt: skip
    r = await call(mcp, "finding_create", args | {"hypotheses": {"h1": "for", "h2": "Against"}})
    assert not r.is_error, text(r)
    assert json.loads(text(r))["hypotheses"] == [
        {"id": "h1", "stance": "for"}, {"id": "h2", "stance": "against"}]  # fmt: skip
    bad = await call(mcp, "finding_create", args | {"hypotheses": ["h1"]})
    assert bad.is_error and "hypotheses: give a list of {id, stance}" in text(bad)
    legacy = await call(mcp, "finding_create", args | {"hypothesis": "h1", "stance": "for"})
    assert json.loads(text(legacy))["hypotheses"] == [{"id": "h1", "stance": "for"}]
    ok = await call(mcp, "hypothesis_update", {"id": "h2", "status": "refuted"})
    assert not ok.is_error, text(ok)


# --- qy7q: hypothesis scope -------------------------------------------------------------------


async def test_mcp_hypothesis_create_takes_the_payment_runs_scope_as_text(tmp_path):
    s = make_service(tmp_path)
    mcp = build_mcp(s, "http://x")
    r = await call(mcp, "hypothesis_create", {"statement": "payment fails", "scope": PAYMENT_SCOPE})
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["scope"] == {"text": PAYMENT_SCOPE}
    assert "stored as text" in out["read_as"][0] and "not checked" in out["read_as"][0]
    assert s.ws.objects.get_hypothesis("h1").scope == HypothesisScope(text=PAYMENT_SCOPE)


async def test_mcp_hypothesis_create_reads_a_structured_scope_like_a_findings(tmp_path):
    s = make_service(tmp_path)
    mcp = build_mcp(s, "http://x")
    sc = {"selector": 'traces_span_metrics_calls_total{service_name="payment"}',
          "range": ["2026-10-03T14:30:00Z", "2026-10-03T15:00:00Z"], "source": "default"}  # fmt: skip
    r = await call(mcp, "hypothesis_create", {"statement": "payment fails", "scope": sc})
    assert not r.is_error, text(r)
    h = s.ws.objects.get_hypothesis("h1")
    assert h.scope is not None and h.scope.selector == sc["selector"]
    assert h.scope.time_range is not None and h.scope.step is None and h.scope.source == "default"
    assert s.log.since(0)[-1].payload["scope"]["selector"] == sc["selector"]
    bad = await call(mcp, "hypothesis_create", {"statement": "x", "scope": {"selector": "up"}})
    assert bad.is_error and "scope.start: field required" in text(bad)
    bad = await call(mcp, "hypothesis_create", {"statement": "x", "scope": {"service": "payment"}})
    assert bad.is_error and "unknown field(s) service" in text(bad)


async def test_a_scope_selector_names_the_hypothesis_subject(tmp_path):
    from .demo_source import DemoSource

    s = make_service(tmp_path, DemoSource())
    await s.query('traces_span_metrics_calls_total{service_name="payment"}', start="now-1h",
                  end="now")  # fmt: skip
    vague = "the failing service is overloaded"
    assert s.ws.hypothesis_subjects(vague) == []
    sc = HypothesisScope(selector='x{service_name="payment"}',
                         time_range={"start_ms": NOW - 1000, "end_ms": NOW})  # fmt: skip
    assert s.ws.hypothesis_subjects(vague, sc) == ['service_name="payment"']
    assert s.ws.hypothesis_subjects(vague, HypothesisScope(text="payment")) == []


def test_hypothesis_scope_validates():
    with pytest.raises(ValidationError, match="either text"):
        HypothesisScope(text="a", selector="up")
    with pytest.raises(ValidationError, match="needs selector, start and end"):
        HypothesisScope(selector="up")
    with pytest.raises(ValidationError, match="step must be"):
        HypothesisScope(selector="up", time_range={"start_ms": 0, "end_ms": 1}, step="auto")
