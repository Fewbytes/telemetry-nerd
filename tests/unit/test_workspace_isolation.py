import pytest

from telemetry_nerd.core.events import EventLog
from telemetry_nerd.model.errors import WrongWorkspace
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.models import AnnotationIn, FindingIn, GapIn
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.registry import WorkspaceRegistry
from telemetry_nerd.workspace.scope import ActiveWorkspace
from telemetry_nerd.workspace.store import WorkspaceStore

SCOPE = {
    "source": "default",
    "selector": "up",
    "step": "1m",
    "aggregation": "avg",
    "time_range": {"start_ms": 0, "end_ms": 60_000},
}


@pytest.fixture
def stores(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    active = ActiveWorkspace("w1")
    ws = WorkspaceStore(con, scope=active)
    return active, ws, ObjectStore(con, ws.next_id, scope=active), EventLog(con, scope=active)


def _finding(objects, panel="p1"):
    data = FindingIn(claim="c", scope=SCOPE, evidence=[{"kind": "panel", "panel": panel}])
    return objects.create_finding(data, "claude")


def test_lists_are_disjoint_and_ids_global(stores):
    active, ws, objects, _ = stores
    p1 = ws.create_panel("q1", {}, [])
    with active.using("w2"):
        p2 = ws.create_panel("q2", {}, [])
        h = objects.create_hypothesis("db is slow", "claude")
        assert [p.id for p in ws.list_panels()] == [p2.id]
    assert p1.id != p2.id
    assert [p.id for p in ws.list_panels()] == [p1.id]
    assert objects.list_hypotheses() == []
    assert objects.get_hypothesis(h.id).id == h.id  # get by id is global
    assert ws.get_panel(p2.id).id == p2.id


def test_cross_workspace_update_is_refused(stores):
    active, ws, _, _ = stores
    p = ws.create_panel("q1", {}, [])
    with active.using("w2"):
        with pytest.raises(WrongWorkspace, match="w1"):
            ws.close_panel(p.id)
        with pytest.raises(WrongWorkspace):
            ws.set_spec(p.id, {"x": 1})
        with pytest.raises(WrongWorkspace):
            ws.set_answered(p.id, "f1")
    assert not ws.get_panel(p.id).closed
    assert ws.close_panel(p.id).closed  # same workspace: allowed


def test_wrong_workspace_message_names_object_workspace_title_and_remedy(stores):
    active, ws, *_ = stores
    p = ws.create_panel("q1", {}, [])
    with active.using("w2"), pytest.raises(WrongWorkspace) as err:
        ws.close_panel(p.id)
    assert str(err.value) == (
        f"{p.id} belongs to workspace w1 'Workspace 1'; workspace_switch to it first"
    )
    assert isinstance(err.value, ValueError)


def test_wrong_workspace_without_known_title_names_the_id_only(stores):
    active, _, objects, _ = stores
    with active.using("w9"):  # not in the workspaces table
        h = objects.create_hypothesis("h", "claude")
    with pytest.raises(WrongWorkspace) as err:
        objects.set_hypothesis_status(h.id, "supported")
    assert str(err.value) == f"{h.id} belongs to workspace w9; workspace_switch to it first"


def test_object_updates_are_refused_across_workspaces(stores):
    active, _, objects, _ = stores
    h = objects.create_hypothesis("h", "claude")
    f = _finding(objects)
    a = objects.create_annotation(AnnotationIn(kind="note", panel="p1", label="x"), "user")
    c = objects.create_code("1", [], "claude")
    t = objects.create_thread(None, None, "user")
    with active.using("w2"):
        for call in (
            lambda: objects.set_hypothesis_status(h.id, "supported"),
            lambda: objects.link_evidence(h.id, f.id, "for"),
            lambda: objects.set_verdict(f.id, "accepted", None),
            lambda: objects.delete_annotation(a.id),
            lambda: objects.finish_code(c.id, status="ok"),
            lambda: objects.add_message(t.id, "hi", "user"),
        ):
            with pytest.raises(WrongWorkspace, match="w1"):
                call()
    assert objects.get_hypothesis(h.id).status == "proposed"
    assert objects.get_thread(t.id).messages == []


def test_owner_reports_the_workspace_or_none(stores):
    active, ws, objects, _ = stores
    with active.using("w2"):
        p = ws.create_panel("q", {}, [])
        h = objects.create_hypothesis("h", "claude")
    assert ws.owner(p.id) == "w2"
    assert objects.owner(h.id) == "w2"
    assert ws.owner("p999") is None
    assert objects.owner("h999") is None


def test_every_insert_records_its_workspace(stores):
    active, ws, objects, log = stores
    with active.using("w2"):
        ws.create_panel("q", {}, [])
        objects.create_annotation(AnnotationIn(kind="note", panel="p1", label="x"), "user")
        log.append("user", "focus.changed", None, {"start_ms": 1, "end_ms": 2})
    con = ws.connection
    assert {r[0] for r in con.execute("SELECT workspace FROM panels")} == {"w2"}
    assert {r[0] for r in con.execute("SELECT workspace FROM objects")} == {"w2"}
    assert {r[0] for r in con.execute("SELECT workspace FROM events")} == {"w2"}


def test_since_filters_but_channel_peek_is_global(stores):
    active, _, _, log = stores
    log.append("user", "thread.message", None, {"thread": "t1", "text": "a", "message": "m1"})
    with active.using("w2"):
        log.append("user", "thread.message", None, {"thread": "t2", "text": "b", "message": "m2"})
        assert [e.workspace for e in log.since(0)] == ["w2"]
    intentional, _, _ = log.peek("claude")
    assert [e.workspace for e in intentional] == ["w1", "w2"]
    assert log.last_seq == 2


def test_claim_cursor_and_ack_are_global(stores):
    active, _, _, log = stores
    log.append("user", "thread.message", None, {"thread": "t1", "text": "a", "message": "m1"})
    with active.using("w2"):
        intentional, _ = log.claim("claude")
        assert [e.seq for e in intentional] == [1]
        assert log.cursor("claude") == 1


def test_message_seqs_are_scoped(stores):
    active, _, _, log = stores
    log.append("user", "thread.message", None, {"thread": "t1", "text": "a", "message": "m1"})
    with active.using("w2"):
        log.append("user", "thread.message", None, {"thread": "t2", "text": "b", "message": "m2"})
        assert log.message_seqs() == {"m2": 2}
    assert log.message_seqs() == {"m1": 1}


def test_tail_returns_the_last_events_of_the_workspace_in_order(stores):
    active, _, _, log = stores
    for i in range(3):
        log.append("user", "focus.changed", None, {"i": i})
    with active.using("w2"):
        log.append("user", "focus.changed", None, {"i": 99})
        assert [e.payload["i"] for e in log.tail(5)] == [99]
    assert [e.payload["i"] for e in log.tail(2)] == [1, 2]
    assert [e.seq for e in log.tail(10)] == [1, 2, 3]


def test_fan_out_carries_the_workspace(stores):
    active, _, _, log = stores
    queue = log.subscribe()
    with active.using("w2"):
        log.append("user", "focus.changed", None, {})
        with log.transaction():
            log.append("user", "focus.changed", None, {})
    assert [queue.get_nowait()["workspace"] for _ in range(2)] == ["w2", "w2"]


def test_event_to_dict_has_workspace(stores):
    _, _, _, log = stores
    assert log.append("user", "focus.changed", None, {}).to_dict()["workspace"] == "w1"


def test_annotations_are_scoped_including_by_panel(stores):
    active, _, objects, _ = stores
    objects.create_annotation(AnnotationIn(kind="event", panel="p1", t_start_ms=1), "user")
    with active.using("w2"):
        a2 = objects.create_annotation(AnnotationIn(kind="event", panel="p1", t_start_ms=1), "user")
        assert [a.id for a in objects.list_annotations()] == [a2.id]
        assert [a.id for a in objects.list_annotations(panel="p1")] == [a2.id]
    assert a2.id not in [a.id for a in objects.list_annotations(include_deleted=True)]


def test_findings_are_scoped(stores):
    active, _, objects, _ = stores
    f1 = _finding(objects)
    with active.using("w2"):
        f2 = _finding(objects)
        assert [f.id for f in objects.list_findings()] == [f2.id]
        assert objects.get_finding(f1.id).id == f1.id
    assert [f.id for f in objects.list_findings()] == [f1.id]


def test_gaps_are_scoped(stores):
    active, _, objects, _ = stores
    objects.create_gap(GapIn(missing_signal="m", needed_for="n"), "claude")
    with active.using("w2"):
        assert objects.list_gaps() == []
    assert len(objects.list_gaps()) == 1


def test_groups_are_scoped(stores):
    active, _, objects, _ = stores
    fields = {
        "kind": "use", "key": "k", "source": "default", "author": "claude", "start_ms": 0,
        "end_ms": 1, "step_ms": 1, "basis": "suggestion", "roles": [],
    }  # fmt: skip
    g = objects.create_group(**fields)
    with active.using("w2"):
        assert objects.list_groups() == []
        assert objects.get_group(g.id).id == g.id
        with pytest.raises(WrongWorkspace):
            objects.set_group(g.model_copy(update={"closed": True}))
    assert [x.id for x in objects.list_groups()] == [g.id]


def test_code_is_scoped(stores):
    active, _, objects, _ = stores
    objects.create_code("1", [], "claude")
    with active.using("w2"):
        assert objects.list_code() == []
    assert len(objects.list_code()) == 1


def test_threads_are_scoped_and_get_by_id_keeps_its_messages(stores):
    active, _, objects, _ = stores
    t1 = objects.create_thread(None, None, "user")
    objects.add_message(t1.id, "hello", "user")
    with active.using("w2"):
        t2 = objects.create_thread(None, None, "user")
        assert [t.id for t in objects.list_threads()] == [t2.id]
        assert [m.text for m in objects.get_thread(t1.id).messages] == ["hello"]
    assert [t.id for t in objects.list_threads()] == [t1.id]


def test_settings_delegate_to_the_registry_per_workspace(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    active = ActiveWorkspace("w1")
    store = WorkspaceStore(con, scope=active)
    registry = WorkspaceRegistry(con, store.next_id, lambda: 1)
    w2 = registry.create("second").id
    store = WorkspaceStore(con, scope=active, registry=registry)
    store.set_setting("default_range", "now-3h")
    with active.using(w2):
        assert store.get_setting("default_range", "now-1h") == "now-1h"
        store.set_setting("default_range", "now-6h")
    assert store.get_setting("default_range") == "now-3h"
    assert registry.get_setting(w2, "default_range") == "now-6h"


def test_every_object_kind_is_inserted_into_the_active_workspace(stores):
    active, ws, objects, _ = stores
    fields = {
        "kind": "use", "key": "k", "source": "default", "author": "claude", "start_ms": 0,
        "end_ms": 1, "step_ms": 1, "basis": "suggestion", "roles": [],
    }  # fmt: skip
    with active.using("w2"):
        made = {
            "hypothesis": objects.create_hypothesis("h", "claude").id,
            "finding": _finding(objects).id,
            "gap": objects.create_gap(GapIn(missing_signal="m", needed_for="n"), "claude").id,
            "annotation": objects.create_annotation(
                AnnotationIn(kind="note", panel="p1", label="x"), "user"
            ).id,
            "panel_group": objects.create_group(**fields).id,
            "code": objects.create_code("1", [], "claude").id,
        }
        thread = objects.create_thread(None, None, "user")
        made["thread"] = thread.id
        made["message"] = objects.add_message(thread.id, "hi", "user").id
    rows = dict(ws.connection.execute("SELECT kind, workspace FROM objects"))
    assert set(rows) == set(made)  # every kind ObjectStore creates is covered
    assert set(rows.values()) == {"w2"}
    assert {i for (i,) in ws.connection.execute("SELECT id FROM objects")} == set(made.values())
