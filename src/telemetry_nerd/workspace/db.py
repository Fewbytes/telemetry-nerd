"""SQLite for workspace objects and the event log. One connection per daemon process."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (prefix TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS panels (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL CHECK (length(trim(question)) > 0),
    status TEXT NOT NULL DEFAULT 'open',
    spec TEXT NOT NULL,
    dataset_ids TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('claude', 'user', 'system')),
    type TEXT NOT NULL,
    object_id TEXT,
    klass TEXT NOT NULL CHECK (klass IN ('intentional', 'ambient', 'internal')),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumers (
    name TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL DEFAULT 0,
    heartbeat_ms INTEGER
);
CREATE TABLE IF NOT EXISTS objects (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    anchor TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS objects_kind ON objects (kind, anchor);
"""

_PANEL_COLUMNS = {
    "answered_by": "ALTER TABLE panels ADD COLUMN answered_by TEXT",
    "closed": "ALTER TABLE panels ADD COLUMN closed INTEGER NOT NULL DEFAULT 0",
}


def open_workspace_db(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    existing = {row[1] for row in con.execute("PRAGMA table_info(panels)")}
    for column, ddl in _PANEL_COLUMNS.items():
        if column not in existing:
            con.execute(ddl)
    return con
