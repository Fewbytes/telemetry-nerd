import json

import pytest

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.workspace.registry import WorkspaceRegistry
from telemetry_nerd.workspace.store import WorkspaceStore


def make(tmp_path, now=lambda: 5000):
    store = WorkspaceStore(tmp_path / "w.db")
    return WorkspaceRegistry(store.connection, store.next_id, now), store.connection


def test_create_allocates_w2_and_list_hides_archived(tmp_path):
    reg, _ = make(tmp_path)
    info = reg.create("Checkout latency", "why slow?")
    assert (info.id, info.title, info.question, info.archived) == (
        "w2",
        "Checkout latency",
        "why slow?",
        False,
    )
    assert reg.ids() == ["w1", "w2"]
    reg.update("w2", archived=True)
    assert [i.id for i in reg.list()] == ["w1"]
    assert {i.id for i in reg.list(include_archived=True)} == {"w1", "w2"}
    assert reg.update("w2", title="  New ", question="q").title == "New"
    with pytest.raises(NotFound):
        reg.get("w9")


def test_title_must_not_be_blank(tmp_path):
    reg, _ = make(tmp_path)
    with pytest.raises(ValueError):
        reg.create("   ")
    with pytest.raises(ValueError):
        reg.update("w1", title=" ")


def test_mark_opened_makes_it_active_even_within_one_ms(tmp_path):
    reg, con = make(tmp_path, now=lambda: 5000)
    con.execute("UPDATE workspaces SET opened_at_ms = 5000")
    reg.create("two")
    con.execute("UPDATE workspaces SET opened_at_ms = 5000")
    reg.mark_opened("w2")
    assert reg.active_id() == "w2"
    reg.mark_opened("w1")
    assert reg.active_id() == "w1"
    reg.mark_opened("w2")
    assert reg.active_id() == "w2"


def test_settings_are_per_workspace(tmp_path):
    reg, _ = make(tmp_path)
    reg.create("two")
    reg.set_setting("w1", "default_range", "now-3h")
    assert reg.get_setting("w1", "default_range", None) == "now-3h"
    assert reg.get_setting("w2", "default_range", "dflt") == "dflt"


def test_note_source_records_once_and_never_default(tmp_path):
    reg, _ = make(tmp_path)
    assert reg.sources("w1") == {}
    reg.note_source("w1", "prom", {"kind": "promql", "url": "a"})
    reg.note_source("w1", "prom", {"kind": "promql", "url": "b"})
    assert reg.sources("w1") == {"prom": {"kind": "promql", "url": "a"}}
    reg.note_source("w1", "default", {"kind": "promql", "url": "d"})
    assert "default" not in reg.sources("w1")


def _obj(con, oid, kind, ts, data, anchor=None, deleted=0, ws="w2"):
    con.execute(
        "INSERT INTO objects (id, kind, anchor, deleted, created_at_ms, data, workspace)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (oid, kind, anchor, deleted, ts, json.dumps(data), ws),
    )


def test_counts_are_open_panels_live_objects_and_open_threads(tmp_path):
    reg, con = make(tmp_path)
    reg.create("two")
    for pid, closed in (("p1", 0), ("p2", 1)):
        con.execute(
            "INSERT INTO panels (id, question, spec, dataset_ids, created_at_ms, closed,"
            " workspace) VALUES (?, 'q', '{}', '[]', 7000, ?, 'w2')",
            (pid, closed),
        )
    _obj(con, "h1", "hypothesis", 1, {})
    _obj(con, "h2", "hypothesis", 1, {}, deleted=1)
    _obj(con, "f1", "finding", 1, {})
    _obj(con, "a1", "annotation", 1, {})
    _obj(con, "t1", "thread", 1, {})  # last message by user: open
    _obj(con, "m1", "message", 2, {"author": "user"}, anchor="t1")
    _obj(con, "t2", "thread", 1, {})  # claude answered last: closed
    _obj(con, "m2", "message", 2, {"author": "user"}, anchor="t2")
    _obj(con, "m3", "message", 3, {"author": "claude"}, anchor="t2")
    _obj(con, "t3", "thread", 1, {}, ws="w1")
    _obj(con, "m4", "message", 2, {"author": "user"}, anchor="t3", ws="w1")
    assert reg.get("w2").counts == {
        "panels": 1,
        "hypotheses": 1,
        "findings": 1,
        "open_threads": 1,
    }
    assert reg.get("w1").counts == {
        "panels": 0,
        "hypotheses": 0,
        "findings": 0,
        "open_threads": 1,
    }
    assert reg.get("w2").to_dict()["counts"]["findings"] == 1


def test_last_activity_is_the_newest_event_only(tmp_path):
    reg, con = make(tmp_path)
    reg.create("two")
    assert reg.get("w2").last_activity_ms == 0
    con.execute("UPDATE workspaces SET opened_at_ms = 1")
    con.execute(
        "INSERT INTO panels (id, question, spec, dataset_ids, created_at_ms, workspace)"
        " VALUES ('p1', 'q', '{}', '[]', 99999, 'w2')"
    )
    con.execute(
        "INSERT INTO events (ts_ms, actor, type, klass, payload, workspace)"
        " VALUES (9000, 'user', 't', 'intentional', '{}', 'w2')"
    )
    assert reg.get("w2").last_activity_ms == 9000
    assert reg.list()[0].id == "w2"  # 9000 beats w1's opened_at_ms of 1
