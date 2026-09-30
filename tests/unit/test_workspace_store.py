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
