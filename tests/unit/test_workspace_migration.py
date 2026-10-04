# tests/unit/test_workspace_migration.py
import json
import sqlite3

from telemetry_nerd.workspace.db import open_workspace_db

OLD = """
CREATE TABLE counters (prefix TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE panels (id TEXT PRIMARY KEY, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  spec TEXT NOT NULL, dataset_ids TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
CREATE TABLE objects (id TEXT PRIMARY KEY, kind TEXT NOT NULL, anchor TEXT,
  deleted INTEGER NOT NULL DEFAULT 0, created_at_ms INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('claude', 'user', 'system')), type TEXT NOT NULL,
  object_id TEXT, klass TEXT NOT NULL CHECK (klass IN ('intentional', 'ambient', 'internal')),
  payload TEXT NOT NULL);
CREATE TABLE workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO counters VALUES ('p', 3), ('h', 1);
INSERT INTO panels VALUES ('p3', 'why?', 'open', '{}', '[]', 1000);
INSERT INTO objects VALUES ('h1', 'hypothesis', NULL, 0, 900, '{}');
INSERT INTO events (ts_ms, actor, type, object_id, klass, payload)
  VALUES (800, 'user', 'thread.message', NULL, 'intentional', '{}');
INSERT INTO workspace_settings VALUES ('default_range', 'now-3h');
"""


def old_db(path):
    con = sqlite3.connect(path)
    con.executescript(OLD)
    con.close()


def test_existing_rows_belong_to_w1_and_nothing_is_lost(tmp_path):
    old_db(tmp_path / "workspace.db")
    con = open_workspace_db(tmp_path / "workspace.db")
    assert con.execute("SELECT id, workspace FROM panels").fetchall() == [("p3", "w1")]
    assert con.execute("SELECT id, workspace FROM objects").fetchall() == [("h1", "w1")]
    assert con.execute("SELECT seq, workspace FROM events").fetchall() == [(1, "w1")]


def test_w1_row_carries_old_settings_and_earliest_time(tmp_path):
    old_db(tmp_path / "workspace.db")
    con = open_workspace_db(tmp_path / "workspace.db")
    wid, title, created, archived, settings = con.execute(
        "SELECT id, title, created_at_ms, archived, settings FROM workspaces"
    ).fetchone()
    assert (wid, title, created, archived) == ("w1", "Workspace 1", 800, 0)
    assert json.loads(settings) == {"default_range": "now-3h"}


def test_next_workspace_id_is_w2(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    (n,) = con.execute("SELECT n FROM counters WHERE prefix = 'w'").fetchone()
    assert n == 1


def test_migration_is_idempotent(tmp_path):
    old_db(tmp_path / "workspace.db")
    open_workspace_db(tmp_path / "workspace.db").close()
    con = open_workspace_db(tmp_path / "workspace.db")
    assert con.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (1,)


def test_fresh_db_starts_with_w1(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    assert con.execute("SELECT id FROM workspaces").fetchall() == [("w1",)]


def test_reopen_keeps_a_user_edited_w1_row(tmp_path):
    path = tmp_path / "workspace.db"
    old_db(path)
    con = open_workspace_db(path)
    con.execute(
        "UPDATE workspaces SET title = 'My title', settings = ? WHERE id = 'w1'",
        (json.dumps({"default_range": "now-9h"}),),
    )
    con.close()
    con = open_workspace_db(path)
    title, settings = con.execute(
        "SELECT title, settings FROM workspaces WHERE id = 'w1'"
    ).fetchone()
    assert title == "My title"
    assert json.loads(settings) == {"default_range": "now-9h"}
    assert con.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (1,)


def test_reopen_keeps_a_larger_workspace_counter(tmp_path):
    path = tmp_path / "workspace.db"
    con = open_workspace_db(path)
    con.execute("UPDATE counters SET n = 5 WHERE prefix = 'w'")
    con.close()
    con = open_workspace_db(path)
    assert con.execute("SELECT n FROM counters WHERE prefix = 'w'").fetchone() == (5,)


def test_workspace_indexes_exist(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"panels_workspace", "objects_workspace", "events_workspace"} <= names
