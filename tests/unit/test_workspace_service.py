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


def test_snapshot_messages_carry_their_event_seq(svc):
    t = svc.ws.ask("why?", "user")
    svc.ws.post_message(t.id, "because", "claude")
    msgs = svc.ws.snapshot()["threads"][0]["messages"]
    by_seq = {e.seq: e.payload["message"] for e in events(svc) if e.type == "thread.message"}
    assert [m["seq"] for m in msgs] == sorted(by_seq)
    assert [m["id"] for m in msgs] == [by_seq[s] for s in sorted(by_seq)]


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


def _rows(svc):
    return svc.ws.workspace.connection.execute("SELECT COUNT(*) FROM objects").fetchone()[0]


def test_failure_after_first_write_rolls_back_everything(svc, panel, monkeypatch):
    f = svc.ws.finding_create(finding_in(svc), "claude")
    objects_before, seq_before = _rows(svc), svc.log.last_seq
    q = svc.log.subscribe()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc.ws.workspace, "set_answered", boom)
    with pytest.raises(RuntimeError):
        svc.ws.finding_create(finding_in(svc, answers_panel=panel.id), "claude")
    assert _rows(svc) == objects_before
    assert svc.log.last_seq == seq_before
    assert q.empty()
    assert svc.ws.workspace.get_panel(panel.id).status == "open"

    monkeypatch.undo()
    monkeypatch.setattr(svc.ws.objects, "link_evidence", boom)
    h = svc.ws.hypothesis_create("h", "claude")
    q.get_nowait()
    seq, rows = svc.log.last_seq, _rows(svc)
    with pytest.raises(RuntimeError):
        svc.ws.finding_create(finding_in(svc, hypothesis=h.id, stance="for"), "claude")
    assert (svc.log.last_seq, _rows(svc)) == (seq, rows)
    assert f.id  # earlier finding untouched
    assert q.empty()


def test_subscriber_gets_events_only_after_commit(svc, panel):
    q = svc.log.subscribe()
    with svc.log.transaction():
        svc.log.append("claude", "x.y", None, {})
        assert q.empty()
    assert q.get_nowait()["type"] == "x.y"
    with pytest.raises(ValueError), svc.log.transaction():
        svc.log.append("claude", "x.z", None, {})
        raise ValueError
    assert q.empty()
    svc.ws.hypothesis_create("h", "claude")
    assert q.get_nowait()["type"] == "hypothesis.created"


def test_claim_still_works_after_transactions(svc, panel):
    svc.ws.ask("hi", "user")
    intentional, _ = svc.log.claim("c")
    assert [e.type for e in intentional] == ["thread.message"]


def test_bad_actor_writes_nothing(svc, panel):
    seq, rows = svc.log.last_seq, _rows(svc)
    with pytest.raises(ValueError, match="actor"):
        svc.ws.hypothesis_create("h", "mallory")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="actor"):
        svc.ws.ask("hi", actor="mallory")  # type: ignore[arg-type]
    assert (svc.log.last_seq, _rows(svc)) == (seq, rows)


def test_ask_anchor_validation(svc, panel):
    a = svc.ws.annotate(AnnotationIn(kind="note", panel=panel.id, label="n"), "user")
    h = svc.ws.hypothesis_create("h", "claude")
    f = svc.ws.finding_create(finding_in(svc), "claude")
    g = svc.ws.gap_create(
        GapIn(missing_signal="s", needed_for="n", suggestion={"name": "m", "type": "gauge"}),
        "claude",
    )
    for anchor in (a.id, h.id, f.id, g.id, panel.id):
        assert svc.ws.ask("q", "user", anchor=anchor).anchor == anchor
    seq = svc.log.last_seq
    for bad in ("nonsense", "x1", "", "t1", "p"):
        with pytest.raises(ValueError):
            svc.ws.ask("q", "user", anchor=bad)
    for missing in ("a99", "h99", "f99", "g99", "p99"):
        with pytest.raises(NotFound):
            svc.ws.ask("q", "user", anchor=missing)
    assert svc.log.last_seq == seq


def test_activity_cursor_and_truncation(svc, panel):
    start = svc.log.last_seq
    for i in range(5):
        svc.ws.hypothesis_create(f"h{i}", "claude")
    out = svc.ws.activity(since=start, limit=2)
    assert len(out["events"]) == 2 and out["truncated"] is True
    assert out["next_since"] == out["events"][-1]["seq"] == start + 2
    out2 = svc.ws.activity(since=out["next_since"], limit=50)
    assert [e["seq"] for e in out2["events"]] == [start + 3, start + 4, start + 5]
    assert out2["truncated"] is False and out2["next_since"] == out2["last_seq"]
    empty = svc.ws.activity(since=out2["last_seq"])
    assert empty["events"] == [] and empty["next_since"] == out2["last_seq"]
    assert empty["truncated"] is False


def test_highlight_rejects_unknown_objects(svc):
    with pytest.raises(NotFound):
        svc.ws.highlight("p99", "claude")
    with pytest.raises(ValueError):
        svc.ws.highlight("zz", "claude")


def test_highlight_and_unhighlight_events(svc):
    h = svc.ws.hypothesis_create("saturation", "claude")
    e = svc.ws.highlight(h.id, "claude", note="look", ttl_ms=1000)
    assert (e.type, e.object_id, e.klass) == ("object.highlighted", h.id, "internal")
    assert e.payload == {"note": "look", "ttl_ms": 1000}
    u = svc.ws.highlight(h.id, "user", note="why?", ttl_ms=None)
    assert u.klass == "intentional" and u.payload["ttl_ms"] is None
    c = svc.ws.unhighlight(h.id, "user")
    assert (c.type, c.object_id, c.klass) == ("object.unhighlighted", h.id, "internal")
