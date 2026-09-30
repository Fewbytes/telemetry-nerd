import itertools

import pytest

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.models import AnnotationIn, FindingIn, GapIn, TimeSpan
from telemetry_nerd.workspace.objects import ObjectStore

SCOPE = {
    "source": "default",
    "selector": "up",
    "step": "1m",
    "aggregation": "avg",
    "time_range": {"start_ms": 0, "end_ms": 60_000},
}


@pytest.fixture
def store(tmp_path):
    counters: dict[str, itertools.count] = {}

    def new_id(prefix):
        return f"{prefix}{next(counters.setdefault(prefix, itertools.count(1)))}"

    return ObjectStore(open_workspace_db(tmp_path / "w.db"), new_id, clock=lambda: 7)


def test_annotation_roundtrip_and_soft_delete(store):
    a = store.create_annotation(
        AnnotationIn(kind="event", panel="p1", t_start_ms=5, label="fault"), "claude"
    )
    assert a.id == "a1" and a.author == "claude" and a.created_at_ms == 7
    assert store.get_annotation("a1") == a
    store.create_annotation(AnnotationIn(kind="threshold", value=1.0), "user")
    assert [x.id for x in store.list_annotations(panel="p1")] == ["a1"]
    assert store.delete_annotation("a1").deleted
    assert [x.id for x in store.list_annotations()] == ["a2"]
    assert [x.id for x in store.list_annotations(include_deleted=True)] == ["a1", "a2"]
    assert store.get_annotation("a1").deleted  # still retrievable


def test_hypothesis_status_and_evidence(store):
    h = store.create_hypothesis("DB pool saturated", "claude")
    old, h2 = store.set_hypothesis_status(h.id, "refuted")
    assert (old, h2.status) == ("proposed", "refuted")
    assert store.link_evidence(h.id, "f1", "against").evidence_against == ["f1"]
    assert store.link_evidence(h.id, "f1", "against").evidence_against == ["f1"]  # idempotent
    assert [x.status for x in store.list_hypotheses()] == ["refuted"]


def test_finding_and_verdict(store):
    f = store.create_finding(
        FindingIn(claim="p99 doubled", scope=SCOPE, evidence=[{"kind": "panel", "panel": "p1"}]),
        "claude",
    )
    assert f.id == "f1" and f.verdict is None
    f2 = store.set_verdict("f1", "rejected", "wrong window")
    assert (f2.verdict, f2.verdict_comment) == ("rejected", "wrong window")
    assert store.list_findings()[0].verdict == "rejected"


def test_gap(store):
    g = store.create_gap(
        GapIn(
            missing_signal="in-flight",
            needed_for="littles_law",
            suggestion={"name": "active_requests", "type": "gauge"},
        ),
        "claude",
    )
    assert store.list_gaps() == [g]


def test_threads_and_messages(store):
    t = store.create_thread("p1", TimeSpan(start_ms=1, end_ms=2), "user")
    store.add_message(t.id, "why the dip?", "user")
    store.add_message(t.id, "GC pause; see a3", "claude")
    got = store.get_thread(t.id)
    assert [m.text for m in got.messages] == ["why the dip?", "GC pause; see a3"]
    assert [x.id for x in store.list_threads(anchor="p1")] == [t.id]
    with pytest.raises(NotFound):
        store.add_message("t99", "x", "user")


def test_unknown_ids(store):
    for getter in (store.get_annotation, store.get_hypothesis, store.get_finding, store.get_thread):
        with pytest.raises(NotFound):
            getter("zz9")


def test_link_evidence_stances(store):
    h = store.create_hypothesis("x", "claude")
    assert store.link_evidence(h.id, "f1", "for").evidence_for == ["f1"]
    got = store.link_evidence(h.id, "f2", "against")
    assert (got.evidence_for, got.evidence_against) == (["f1"], ["f2"])
    with pytest.raises(ValueError, match="stance"):
        store.link_evidence(h.id, "f3", "maybe")
    assert store.get_hypothesis(h.id).evidence_against == ["f2"]


def test_bad_status_and_verdict_raise(store):
    h = store.create_hypothesis("x", "claude")
    with pytest.raises(ValueError):
        store.set_hypothesis_status(h.id, "bogus")
    assert store.get_hypothesis(h.id).status == "proposed"
    f = store.create_finding(
        FindingIn(claim="c", scope=SCOPE, evidence=[{"kind": "panel", "panel": "p1"}]), "claude"
    )
    with pytest.raises(ValueError):
        store.set_verdict(f.id, "maybe", None)
    assert store.get_finding(f.id).verdict is None


def test_finding_with_rich_evidence_survives_reload(tmp_path):
    path = tmp_path / "w.db"
    ids = itertools.count(1)
    s1 = ObjectStore(open_workspace_db(path), lambda p: f"{p}{next(ids)}", clock=lambda: 7)
    f = s1.create_finding(
        FindingIn(
            claim="c",
            scope=SCOPE,
            evidence=[
                {
                    "kind": "statistic",
                    "dataset": "d1",
                    "name": "ratio",
                    "value": 2.1,
                    "interval": [1.8, 2.4],
                    "method": "bootstrap",
                    "params": {"n": 100},
                },
                {"kind": "annotation", "annotation": "a1"},
            ],
        ),
        "claude",
    )
    s2 = ObjectStore(open_workspace_db(path), lambda p: "x", clock=lambda: 9)
    assert s2.get_finding(f.id) == f
    assert s2.list_findings() == [f]
