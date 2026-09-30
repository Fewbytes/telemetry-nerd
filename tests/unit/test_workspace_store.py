import pytest

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.workspace.store import WorkspaceStore


@pytest.fixture
def ws(tmp_path):
    return WorkspaceStore(tmp_path / "w.db", clock=lambda: 42)


def test_next_id_is_per_prefix_sequence(ws):
    assert [ws.next_id("d"), ws.next_id("d"), ws.next_id("p")] == ["d1", "d2", "p1"]


def test_ids_survive_reopen(tmp_path):
    WorkspaceStore(tmp_path / "w.db").next_id("d")
    assert WorkspaceStore(tmp_path / "w.db").next_id("d") == "d2"


def test_create_and_get_panel(ws):
    p = ws.create_panel("Is latency up?", {"layers": []}, ["d1"])
    assert p.id == "p1"
    assert p.status == "open"
    got = ws.get_panel("p1")
    assert got == p
    assert got.to_dict()["question"] == "Is latency up?"
    assert got.created_at_ms == 42


@pytest.mark.parametrize("question", ["", "   "])
def test_panel_requires_question(ws, question):
    with pytest.raises(ValueError, match="explicit question"):
        ws.create_panel(question, {}, ["d1"])


def test_list_panels_newest_first(ws):
    ws.create_panel("q1", {}, [])
    ws.create_panel("q2", {}, [])
    assert [p.id for p in ws.list_panels()] == ["p2", "p1"]


def test_unknown_panel(ws):
    with pytest.raises(NotFound):
        ws.get_panel("p9")


def test_answered_and_closed(ws):
    p = ws.create_panel("q", {}, [])
    assert ws.set_answered(p.id, "f1").status == "answered"
    assert ws.get_panel(p.id).answered_by == "f1"
    ws.close_panel(p.id)
    assert ws.list_panels() == []
    assert [x.id for x in ws.list_panels(include_closed=True)] == [p.id]


def test_existing_db_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE panels (id TEXT PRIMARY KEY, question TEXT NOT NULL, status TEXT NOT NULL "
        "DEFAULT 'open', spec TEXT NOT NULL, dataset_ids TEXT NOT NULL, created_at_ms INTEGER NOT NULL);"
        "INSERT INTO panels VALUES ('p1','q','open','{}','[]',1);"
    )
    con.close()
    from telemetry_nerd.workspace.store import WorkspaceStore

    p = WorkspaceStore(path).get_panel("p1")
    assert p.answered_by is None and p.closed is False
