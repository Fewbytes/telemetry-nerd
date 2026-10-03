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
    assert "default" not in reg.sources("w1")


def test_counts_and_last_activity_are_derived(tmp_path):
    reg, con = make(tmp_path)
    reg.create("two")
    con.execute("UPDATE workspaces SET opened_at_ms = 1")
    con.execute(
        "INSERT INTO panels (id, question, spec, dataset_ids, created_at_ms, workspace)"
        " VALUES ('p1', 'q', '{}', '[]', 7000, 'w2')"
    )
    con.execute(
        "INSERT INTO objects (id, kind, created_at_ms, data, workspace)"
        " VALUES ('h1', 'hypothesis', 8000, '{}', 'w2')"
    )
    con.execute(
        "INSERT INTO events (ts_ms, actor, type, klass, payload, workspace)"
        " VALUES (9000, 'user', 't', 'intentional', '{}', 'w2')"
    )
    info = reg.get("w2")
    assert info.counts == {"panel": 1, "hypothesis": 1}
    assert info.last_activity_ms == 9000
    assert reg.get("w1").counts == {}
    assert reg.list()[0].id == "w2"
    assert info.to_dict()["counts"] == {"panel": 1, "hypothesis": 1}
