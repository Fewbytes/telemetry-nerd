import asyncio
import json

import pytest

from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.workspace.models import AnnotationIn, FindingIn, GapIn, TimeSpan

from .fakes import make_service


def scope():
    return {
        "source": "default",
        "selector": "up",
        "step": "1m",
        "aggregation": "avg",
        "time_range": {"start_ms": 0, "end_ms": 60_000},
    }


def finding_in(svc, dataset="d1", **kw):
    ev = [
        {
            "kind": "statistic",
            "dataset": dataset,
            "name": "n",
            "value": 1.0,
            "exact": True,
            "method": "count",
        }
    ]
    return FindingIn(claim="p99 doubled", scope=scope(), evidence=ev, **kw)


@pytest.fixture
def svc(tmp_path):
    return make_service(tmp_path)


@pytest.fixture
def panel(svc):
    """A real dataset d1 and panel p1."""
    asyncio.run(svc.query("up", start="now-1h", end="now"))
    return svc.show("d1", "why?").panel


def events(svc, since=0):
    return svc.log.since(since)


def test_annotate_by_user_is_intentional_and_unknown_panel_rejected(svc, panel):
    a = svc.ws.annotate(AnnotationIn(kind="note", panel=panel.id, label="hi"), "user")
    e = events(svc)[-1]
    assert (e.type, e.object_id, e.klass, e.actor) == (
        "annotation.created",
        a.id,
        "intentional",
        "user",
    )
    assert e.payload == {"kind": "note", "panel": panel.id, "label": "hi"}
    before = svc.log.last_seq
    with pytest.raises(NotFound, match="p99"):
        svc.ws.annotate(AnnotationIn(kind="note", panel="p99", label="x"), "user")
    assert svc.log.last_seq == before


def test_delete_annotation_soft_and_event(svc, panel):
    a = svc.ws.annotate(AnnotationIn(kind="note", panel=panel.id, label="hi"), "user")
    d = svc.ws.delete_annotation(a.id, "user")
    assert d.deleted
    assert events(svc)[-1].type == "annotation.deleted"
    assert events(svc)[-1].payload == {}
    assert svc.ws.snapshot()["annotations"] == []


def test_finding_unknown_dataset_and_answers_panel(svc, panel):
    before = svc.log.last_seq
    with pytest.raises(NotFound, match="dataset d9"):
        svc.ws.finding_create(finding_in(svc, "d9"), "claude")
    with pytest.raises(NotFound, match="p99"):
        svc.ws.finding_create(finding_in(svc, answers_panel="p99"), "claude")
    assert svc.log.last_seq == before

    f = svc.ws.finding_create(finding_in(svc, answers_panel=panel.id), "claude")
    p = svc.workspace.get_panel(panel.id)
    assert p.status == "answered" and p.answered_by == f.id
    evs = events(svc, before)
    assert [e.type for e in evs] == ["finding.created", "panel.answered"]
    assert [e.klass for e in evs] == ["internal", "internal"]
    assert evs[1].object_id == panel.id and evs[1].payload == {"finding": f.id}
    assert evs[0].payload == {
        "claim": "p99 doubled",
        "hypothesis": None,
        "stance": None,
        "answers_panel": panel.id,
    }


def test_finding_missing_refs_rejected(svc, panel):
    for bad in (
        {"evidence": [{"kind": "panel", "panel": "p99"}]},
        {"evidence": [{"kind": "annotation", "annotation": "a99"}]},
        {"hypothesis": "h99", "stance": "for"},
    ):
        data = finding_in(svc).model_dump() | bad
        with pytest.raises(NotFound):
            svc.ws.finding_create(FindingIn(**data), "claude")


def test_finding_linked_to_hypothesis_against(svc, panel):
    h = svc.ws.hypothesis_create("cache cold", "claude")
    f = svc.ws.finding_create(finding_in(svc, hypothesis=h.id, stance="against"), "claude")
    assert svc.ws.objects.get_hypothesis(h.id).evidence_against == [f.id]
    assert svc.ws.objects.get_hypothesis(h.id).evidence_for == []


def test_finding_verdict_by_user_is_intentional(svc, panel):
    f = svc.ws.finding_create(finding_in(svc), "claude")
    out = svc.ws.finding_verdict(f.id, "rejected", "user", comment="wrong window")
    assert out.verdict == "rejected"
    e = events(svc)[-1]
    assert e.klass == "intentional" and e.type == "finding.verdict" and e.object_id == f.id
    assert e.payload == {"verdict": "rejected", "comment": "wrong window", "claim": "p99 doubled"}


def test_hypothesis_update_records_transition_and_stays_visible(svc):
    h = svc.ws.hypothesis_create("cache cold", "claude")
    assert events(svc)[-1].type == "hypothesis.created"
    assert events(svc)[-1].payload == {"statement": "cache cold"}
    svc.ws.hypothesis_update(h.id, "refuted", "user", note="nope")
    e = events(svc)[-1]
    assert e.type == "hypothesis.status_changed" and e.klass == "intentional"
    assert e.payload == {"from": "proposed", "to": "refuted", "note": "nope"}
    hyps = svc.ws.snapshot()["hypotheses"]
    assert [(x["id"], x["status"]) for x in hyps] == [(h.id, "refuted")]
    with pytest.raises(NotFound):
        svc.ws.hypothesis_update("h99", "refuted", "user")


def test_gap_create(svc):
    g = svc.ws.gap_create(
        GapIn(
            missing_signal="queue depth",
            needed_for="saturation",
            suggestion={"name": "queue_depth", "type": "gauge"},
        ),
        "claude",
    )
    e = events(svc)[-1]
    assert (e.type, e.object_id, e.klass) == ("gap.created", g.id, "internal")
    assert e.payload == {"missing_signal": "queue depth", "needed_for": "saturation"}


def test_ask_and_post_message(svc, panel):
    sel = TimeSpan(start_ms=0, end_ms=300_000)
    t = svc.ws.ask("why the dip?", "user", anchor=panel.id, selection=sel)
    assert t.id.startswith("t") and [m.text for m in t.messages] == ["why the dip?"]
    e = events(svc)[-1]
    assert e.type == "thread.message" and e.klass == "intentional" and e.object_id == t.id
    assert e.payload["anchor"] == panel.id
    assert e.payload["selection"] == {"start_ms": 0, "end_ms": 300_000}
    assert e.payload["text"] == "why the dip?" and e.payload["thread"] == t.id

    m = svc.ws.post_message(t.id, "checking", "claude")
    e2 = events(svc)[-1]
    assert e2.klass == "internal" and e2.actor == "claude" and e2.payload["message"] == m.id
    assert e2.payload["anchor"] == panel.id
    assert [x.text for x in svc.ws.objects.get_thread(t.id).messages] == [
        "why the dip?",
        "checking",
    ]
    with pytest.raises(NotFound):
        svc.ws.post_message("t99", "x", "claude")
    with pytest.raises(NotFound):
        svc.ws.ask("x", "user", anchor="p99")


def test_close_panel_and_focus(svc, panel):
    svc.ws.close_panel(panel.id, "user")
    e = events(svc)[-1]
    assert (e.type, e.object_id, e.klass, e.payload) == ("panel.closed", panel.id, "ambient", {})
    assert svc.ws.list_panels() == []
    assert svc.ws.set_focus(TimeSpan(start_ms=1, end_ms=2), "user") is None
    e = events(svc)[-1]
    assert (e.type, e.object_id, e.payload) == ("focus.changed", None, {"start_ms": 1, "end_ms": 2})


def test_snapshot_keys_and_exclusions(svc, panel):
    a = svc.ws.annotate(AnnotationIn(kind="note", panel=panel.id, label="hi"), "user")
    svc.ws.delete_annotation(a.id, "user")
    p2 = svc.show("d1", "second?").panel
    svc.ws.close_panel(p2.id, "user")
    snap = svc.ws.snapshot()
    assert set(snap) == {
        "panels",
        "annotations",
        "hypotheses",
        "findings",
        "gaps",
        "threads",
        "last_seq",
    }
    assert [p["id"] for p in snap["panels"]] == [panel.id]
    assert snap["annotations"] == []
    assert snap["last_seq"] == svc.log.last_seq
    json.dumps(snap)


def test_brief_under_budget_truncates_and_keeps_open_threads(svc, panel):
    for i in range(50):
        svc.ws.finding_create(
            finding_in(svc).model_copy(update={"claim": f"claim {i} " + "x" * 80}), "claude"
        )
        svc.ws.ask(f"question {i} " + "y" * 150, "user")
    t = svc.ws.ask("answered one", "user")
    svc.ws.post_message(t.id, "done", "claude")
    b = svc.ws.brief()
    assert len(json.dumps(b)) <= 4096
    assert b["more_findings"] > 0 or b["more_open_threads"] > 0
    assert b["last_seq"] == svc.log.last_seq
    assert all(x["id"] != t.id for x in b["open_threads"])
    assert b["open_threads"] and b["open_threads"][0]["last"].startswith("question")


def test_brief_small_workspace_has_no_more_keys(svc, panel):
    svc.ws.ask("hello there", "user", anchor=panel.id)
    b = svc.ws.brief()
    assert not [k for k in b if k.startswith("more_")]
    assert b["open_threads"][0]["last"] == "hello there"
    assert b["panels"][0]["id"] == panel.id


def test_activity_since(svc, panel):
    start = svc.log.last_seq
    svc.ws.hypothesis_create("h", "claude")
    svc.ws.ask("hey", "user")
    out = svc.ws.activity(since=start)
    assert out["last_seq"] == svc.log.last_seq
    assert [e["type"] for e in out["events"]] == ["hypothesis.created", "thread.message"]
    for row, e in zip(out["events"], events(svc, start), strict=True):
        assert set(row) == {"seq", "actor", "type", "object_id", "summary"}
        assert row["summary"] == describe_event(e)
    assert len(svc.ws.activity(limit=1)["events"]) == 1
