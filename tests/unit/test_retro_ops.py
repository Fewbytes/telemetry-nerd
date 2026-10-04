"""Retrospective operations (spec 2026-10-04): proposals, lessons, decisions, surfacing."""

from __future__ import annotations

import pytest

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.retro.models import DEFAULT_EXPIRY_MS, LessonScope, expires_at
from telemetry_nerd.workspace.models import FindingIn
from tests.unit.fakes import NOW, make_service

DAY = 86_400_000


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> int:
        return self.t


def finding(svc, selector: str, source: str = "default", verdict: str | None = None) -> str:
    p = svc.ws.workspace.create_panel("q", {}, [])
    f = svc.ws.objects.create_finding(
        FindingIn(
            claim="c",
            scope={
                "source": source,
                "selector": selector,
                "step": "1m",
                "aggregation": "avg",
                "time_range": {"start_ms": 0, "end_ms": 60_000},
            },
            evidence=[{"kind": "panel", "panel": p.id}],
        ),
        "claude",
    )
    if verdict:
        svc.ws.finding_verdict(f.id, verdict, "user")
    return f.id


def scope(**kw) -> LessonScope:
    return LessonScope.model_validate({"source": "default", **kw})


def events(svc, type_):
    return [e for e in svc.log.since(0) if e.type == type_]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(tmp_path, clock):
    s = make_service(tmp_path, clock=clock)
    s.ws.catalog.relearn("default", ["checkout_latency", "queue_depth"], NOW)
    return s


def test_lesson_needs_evidence_that_exists(svc):
    with pytest.raises(ValueError, match="lesson_without_evidence"):
        svc.retro.lesson_propose("split by region", scope(service="checkout"), [])
    with pytest.raises(NotFound):
        svc.retro.lesson_propose("split by region", scope(service="checkout"), ["f999"])
    with pytest.raises(ValueError, match="finding or panel ids"):
        svc.retro.lesson_propose("split by region", scope(service="checkout"), ["h1"])


def test_lesson_scope_guard_refuses_overgeneralising(svc):
    f1 = finding(svc, 'checkout_latency{service_name="checkout"}')
    with pytest.raises(ValueError, match="lesson_beyond_evidence"):
        svc.retro.lesson_propose("split by region", scope(), [f1])
    lesson = svc.retro.lesson_propose("split by region", scope(service="checkout"), [f1])
    assert lesson.status == "proposed" and lesson.scope_check.covered_by == [f1]
    assert lesson.expires_at_ms == NOW + DEFAULT_EXPIRY_MS
    (e,) = events(svc, "lesson.proposed")
    assert e.klass == "internal" and e.object_id == lesson.id


def test_rejected_finding_is_not_evidence(svc):
    f1 = finding(svc, 'x{service_name="checkout"}', verdict="rejected")
    with pytest.raises(ValueError, match="was rejected"):
        svc.retro.lesson_propose("t", scope(service="checkout"), [f1])


async def test_panel_evidence_reads_its_dataset_expressions(svc):
    empty = svc.ws.workspace.create_panel("q", {}, [])
    with pytest.raises(ValueError, match="draws no dataset"):
        svc.retro.lesson_propose("t", scope(), [empty.id])
    out = await svc.query('rate(x{service_name="checkout"}[5m])', start="now-1h", end="now")
    p = svc.ws.workspace.create_panel("q", {}, [out["dataset"]])
    with pytest.raises(ValueError, match="not the whole source"):
        svc.retro.lesson_propose("t", scope(), [p.id])
    lesson = svc.retro.lesson_propose("t", scope(service="checkout"), [p.id])
    assert lesson.scope_check.covered_by == [p.id]


def test_duplicate_lesson_refused(svc):
    f1 = finding(svc, 'x{service_name="checkout"}')
    svc.retro.lesson_propose("Split by region", scope(service="checkout"), [f1])
    with pytest.raises(ValueError, match="already on file"):
        svc.retro.lesson_propose("split by region", scope(service="checkout"), [f1])


def test_user_decides_lessons_and_edits_are_kept(svc):
    f1 = finding(svc, 'x{service_name="checkout"}')
    lesson = svc.retro.lesson_propose("split by region", scope(service="checkout"), [f1])
    with pytest.raises(ValueError, match="only the user"):
        svc.retro.decide_lesson(lesson.id, "approve", "claude")
    out = svc.retro.decide_lesson(
        lesson.id, "approve", "user", text="split checkout latency by region", expires="30d"
    )
    assert out.status == "approved" and out.proposed_text == "split by region"
    assert out.expires_at_ms == NOW + 30 * DAY
    with pytest.raises(ValueError, match="already approved"):
        svc.retro.decide_lesson(lesson.id, "reject", "user")
    (e,) = events(svc, "lesson.decided")
    assert e.klass == "intentional" and e.payload["edited"] is True


def test_lessons_for_matches_scope_and_nothing_else(svc, clock):
    f1 = finding(svc, 'x{service_name="checkout"}')
    f2 = finding(svc, "x")
    svc_lesson = svc.retro.lesson_propose("svc", scope(service="checkout"), [f1])
    wide = svc.retro.lesson_propose("wide", scope(), [f2])
    fam = svc.retro.lesson_propose("fam", scope(metric_family="x"), [f2])
    pending = svc.retro.lesson_propose("pending", scope(labels={"namespace": "a"}), [f2])
    for x in (svc_lesson, wide, fam):
        svc.retro.decide_lesson(x.id, "approve", "user")

    out = svc.retro.lessons_for("default")
    assert [x["id"] for x in out["lessons"]] == [wide.id] and out["held"] == 2
    out = svc.retro.lessons_for("default", services=["checkout"], metric_families=["x"])
    assert {x["id"] for x in out["lessons"]} == {svc_lesson.id, wide.id, fam.id}
    assert out["held"] == 0
    row = next(x for x in out["lessons"] if x["id"] == svc_lesson.id)
    assert row["scope"] == {"source": "default", "service": "checkout"}
    assert row["evidence"] == [{"id": f1, "workspace": "w1"}]
    assert svc.retro.lessons_for("default", services=["cart"])["held"] == 2
    assert svc.retro.lessons_for("other")["lessons"] == []
    assert pending.id not in {x["id"] for x in svc.retro.lessons_for("default")["lessons"]}

    clock.t = wide.expires_at_ms  # expiry is derived on read
    assert svc.retro.store.lesson(wide.id).state(clock.t) == "expired"
    assert wide.id not in {x["id"] for x in svc.retro.lessons_for("default")["lessons"]}


async def test_lessons_persist_across_workspaces(svc):
    f1 = finding(svc, "x")
    lesson = svc.retro.lesson_propose("wide", scope(), [f1])
    svc.retro.decide_lesson(lesson.id, "approve", "user")
    await svc.workspaces.create("next investigation", None, "claude")
    out = svc.retro.lessons_for("default")
    assert [x["id"] for x in out["lessons"]] == [lesson.id]
    assert out["lessons"][0]["evidence"] == [{"id": f1, "workspace": "w1"}]


def test_refute_needs_an_overlapping_finding_from_claude(svc):
    f1 = finding(svc, 'x{service_name="checkout"}')
    lesson = svc.retro.lesson_propose("svc", scope(service="checkout"), [f1])
    with pytest.raises(ValueError, match="only an approved lesson"):
        svc.retro.refute_lesson(lesson.id, [f1], "no", "claude")
    svc.retro.decide_lesson(lesson.id, "approve", "user")
    other = finding(svc, 'x{service_name="cart"}')
    with pytest.raises(ValueError, match="no cited finding is about"):
        svc.retro.refute_lesson(lesson.id, [other], "cart differs", "claude")
    with pytest.raises(ValueError, match="cite the finding"):
        svc.retro.refute_lesson(lesson.id, [], "because", "claude")
    with pytest.raises(ValueError, match="not panels"):
        svc.retro.refute_lesson(lesson.id, ["p1"], "because", "claude")
    against = finding(svc, 'x{service_name="checkout", region="eu"}')
    out = svc.retro.refute_lesson(lesson.id, [against], "region no longer matters", "claude")
    assert out.status == "refuted" and out.refuted_by == [against]
    assert svc.retro.lessons_for("default", services=["checkout"])["lessons"] == []


def test_user_refutes_with_a_reason_only(svc):
    f1 = finding(svc, "x")
    lesson = svc.retro.lesson_propose("wide", scope(), [f1])
    svc.retro.decide_lesson(lesson.id, "approve", "user")
    with pytest.raises(ValueError, match="reason is required"):
        svc.retro.refute_lesson(lesson.id, [], " ", "user")
    out = svc.retro.refute_lesson(lesson.id, [], "we re-architected", "user")
    assert out.status == "refuted"
    (e,) = events(svc, "lesson.refuted")
    assert e.klass == "intentional"


def test_expires_parsing():
    assert expires_at(None, 0) == DEFAULT_EXPIRY_MS
    assert expires_at("90d", 0) == 90 * DAY
    now = 1_790_000_000_000  # 2026-09-21
    assert expires_at("2027-03-01", now) == 1_803_859_200_000
    assert expires_at("2027-03-01T00:00:00Z", now) == 1_803_859_200_000
    for bad, msg in (("yesterday", "duration"), ("1000d", "two years"), ("2026-01-02", "future")):
        with pytest.raises(ValueError, match=msg):
            expires_at(bad, now)


def item(**kw) -> dict:
    return {"metric": "checkout_latency", "field": "unit", "value": "s", "confidence": 0.8,
            "basis": "histogram buckets in seconds", **kw}  # fmt: skip


def test_catalog_proposals_are_checked_one_by_one(svc):
    f1 = finding(svc, "checkout_latency")
    res = svc.retro.catalog_propose(
        "default",
        [
            item(evidence=[f1]),
            item(),  # duplicate of the first, still open
            item(metric="nope"),
            item(field="bounds", value="sometimes"),
            item(confidence=1.0),
            item(basis=""),
            item(metric="queue_depth", field="type", value="gauge", evidence=["f999"]),
        ],
    )
    assert [r["status"] for r in res] == ["proposed"] + ["rejected"] * 6
    assert "already proposed as cp" in res[1]["reason"]
    assert "unknown metric" in res[2]["reason"]
    assert "f999" in res[6]["reason"]
    # nothing reached the catalog
    assert "unit" not in svc.ws.catalog_entry("default", "checkout_latency").fields


def test_approving_a_proposal_writes_a_claude_claim_verified_by_user(svc):
    f1 = finding(svc, "checkout_latency")
    (r,) = svc.retro.catalog_propose("default", [item(evidence=[f1])])
    with pytest.raises(ValueError, match="only the user"):
        svc.retro.decide_proposal(r["proposal"], "approve", "claude")
    p = svc.retro.decide_proposal(r["proposal"], "approve", "user")
    assert p.status == "approved" and not p.edited
    claim = svc.ws.catalog_entry("default", "checkout_latency").fields["unit"]
    assert (claim.origin, claim.verified_by, claim.value) == ("claude", "user", "s")
    assert r["proposal"] in claim.citation and f1 in claim.citation
    (e,) = events(svc, "proposal.decided")
    assert e.klass == "intentional" and e.actor == "user"
    # the same value, now confirmed, is not proposed again
    (again,) = svc.retro.catalog_propose("default", [item()])
    assert "already holds" in again["reason"]


def test_edited_approval_is_the_users_claim_and_reject_writes_nothing(svc):
    a, b = svc.retro.catalog_propose(
        "default", [item(), item(metric="queue_depth", field="type", value="counter")]
    )
    p = svc.retro.decide_proposal(a["proposal"], "approve", "user", value="ms")
    assert p.edited and p.decided_value == "ms"
    claim = svc.ws.catalog_entry("default", "checkout_latency").fields["unit"]
    assert (claim.origin, claim.value, claim.confidence) == ("user", "ms", 1.0)
    svc.retro.decide_proposal(b["proposal"], "reject", "user", comment="it is a gauge")
    assert "type" not in svc.ws.catalog_entry("default", "queue_depth").fields
    with pytest.raises(ValueError, match="invalid bounds"):
        (c,) = svc.retro.catalog_propose("default", [item(field="bounds", value="≥0")])
        svc.retro.decide_proposal(c["proposal"], "approve", "user", value="bogus")


def test_listing_and_summary(svc):
    f1 = finding(svc, "x")
    lesson = svc.retro.lesson_propose("wide", scope(), [f1])
    svc.retro.catalog_propose("default", [item()])
    out = svc.retro.listing()
    assert out["pending"] == 2 and out["lessons"][0]["state"] == "proposed"
    assert out["lessons"][0]["evidence_where"] == {f1: "w1"}
    assert svc.retro.summary() == {"lessons": {}, "pending": 2}
    svc.retro.decide_lesson(lesson.id, "approve", "user")
    assert svc.retro.summary({"default"}) == {"lessons": {"default": 1}, "pending": 1}
    assert svc.retro.summary({"other"}) == {"lessons": {}, "pending": 1}
    assert [x["id"] for x in svc.retro.listing("approved")["lessons"]] == [lesson.id]
