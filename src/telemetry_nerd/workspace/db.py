"""SQLite for workspace objects and the event log. One connection per daemon process."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_EVENTS_DDL = """CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    actor TEXT NOT NULL CHECK (actor IN ('claude', 'user', 'system', 'code')),
    type TEXT NOT NULL,
    object_id TEXT,
    klass TEXT NOT NULL CHECK (klass IN ('intentional', 'ambient', 'internal')),
    payload TEXT NOT NULL
)"""

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
CREATE TABLE IF NOT EXISTS catalog_metrics (
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    family TEXT,
    dimension TEXT,
    is_family INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, metric)
);
CREATE TABLE IF NOT EXISTS catalog_families (
    source TEXT NOT NULL,
    template TEXT NOT NULL,
    members INTEGER NOT NULL,
    distinct_dims INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'detected',
    decided_by TEXT,
    basis TEXT,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (source, template)
);
CREATE TABLE IF NOT EXISTS catalog_family_rejections (
    source TEXT NOT NULL,
    template TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (source, template)
);
CREATE TABLE IF NOT EXISTS catalog_claims (
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    field TEXT NOT NULL,
    origin TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL,
    verified_by TEXT,
    citation TEXT,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (source, metric, field, origin)
);
CREATE TABLE IF NOT EXISTS catalog_relations (
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,
    object TEXT NOT NULL,
    origin TEXT NOT NULL,
    confidence REAL NOT NULL,
    retracted INTEGER NOT NULL DEFAULT 0,
    params TEXT NOT NULL,
    basis TEXT,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (level, source, subject, kind, object, origin)
);
CREATE TABLE IF NOT EXISTS catalog_bindings (
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    origin TEXT NOT NULL,
    confidence REAL NOT NULL,
    retracted INTEGER NOT NULL DEFAULT 0,
    roles TEXT NOT NULL,
    join_on TEXT NOT NULL,
    basis TEXT,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (level, source, kind, key, origin)
);
CREATE TABLE IF NOT EXISTS catalog_binding_gaps (
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    role TEXT NOT NULL,
    gap_id TEXT NOT NULL,
    PRIMARY KEY (level, source, kind, key, role)
);
CREATE TABLE IF NOT EXISTS operating_profiles (
    id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    expr TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    computed_at_ms INTEGER NOT NULL,
    data TEXT,
    error TEXT,
    failed_at_ms INTEGER,
    PRIMARY KEY (source, expr)
);
CREATE TABLE IF NOT EXISTS catalog_samples (
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    window_ms INTEGER NOT NULL,
    step_ms INTEGER NOT NULL,
    series INTEGER NOT NULL,
    voting INTEGER NOT NULL,
    n INTEGER NOT NULL,
    min REAL,
    max REAL,
    negatives INTEGER NOT NULL,
    increases INTEGER NOT NULL,
    decreases INTEGER NOT NULL,
    resets INTEGER NOT NULL,
    small_decreases INTEGER NOT NULL,
    gauge_voters INTEGER NOT NULL,
    integral INTEGER NOT NULL,
    constant INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    dataset TEXT NOT NULL,
    scanned_ms INTEGER NOT NULL,
    PRIMARY KEY (source, metric)
);
CREATE TABLE IF NOT EXISTS catalog_findings (
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    kind TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    PRIMARY KEY (source, metric, kind)
);
CREATE TABLE IF NOT EXISTS label_listings (
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    metric TEXT NOT NULL DEFAULT '',
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    label_values TEXT NOT NULL,
    truncated INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    PRIMARY KEY (source, label, metric, start_ms, end_ms)
);
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    question TEXT,
    created_at_ms INTEGER NOT NULL,
    opened_at_ms INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    settings TEXT NOT NULL DEFAULT '{}',
    sources TEXT NOT NULL DEFAULT '{}'
);
"""

_PANEL_COLUMNS = {
    "answered_by": "ALTER TABLE panels ADD COLUMN answered_by TEXT",
    "closed": "ALTER TABLE panels ADD COLUMN closed INTEGER NOT NULL DEFAULT 0",
}


_METRIC_COLUMNS = {
    "family": "ALTER TABLE catalog_metrics ADD COLUMN family TEXT",
    "dimension": "ALTER TABLE catalog_metrics ADD COLUMN dimension TEXT",
    "is_family": "ALTER TABLE catalog_metrics ADD COLUMN is_family INTEGER NOT NULL DEFAULT 0",
}


# Workspace-scoped tables (spec D2). The default exists only for rows written before
# workspaces; every insert passes the workspace explicitly.
_WORKSPACE_TABLES = ("panels", "objects", "events")
_WORKSPACE_COLUMN = "ALTER TABLE {table} ADD COLUMN workspace TEXT NOT NULL DEFAULT 'w1'"
_WORKSPACE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS panels_workspace ON panels (workspace)",
    "CREATE INDEX IF NOT EXISTS objects_workspace ON objects (workspace, kind, anchor)",
    "CREATE INDEX IF NOT EXISTS events_workspace ON events (workspace, seq)",
)


def _seed_w1(con: sqlite3.Connection) -> None:
    """Existing data becomes workspace w1 (spec Migration): its creation time is the earliest
    event or panel, its settings the old workspace_settings rows. Ids continue at w2."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT 1 FROM workspaces LIMIT 1").fetchone() is None:
            (earliest,) = con.execute(
                "SELECT MIN(t) FROM (SELECT MIN(ts_ms) AS t FROM events"
                " UNION ALL SELECT MIN(created_at_ms) FROM panels)"
            ).fetchone()
            now = int(time.time() * 1000)
            settings = dict(con.execute("SELECT key, value FROM workspace_settings"))
            con.execute(
                "INSERT INTO workspaces (id, title, created_at_ms, opened_at_ms, settings)"
                " VALUES ('w1', 'Workspace 1', ?, ?, ?)",
                (earliest if earliest is not None else now, now, json.dumps(settings)),
            )
        con.execute("INSERT INTO counters VALUES ('w', 1) ON CONFLICT DO UPDATE SET n = MAX(n, 1)")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _migrate_event_actors(con: sqlite3.Connection) -> None:
    """Databases created before tier-2 runs reject actor 'code' (spec §2.3): SQLite cannot
    alter a CHECK, so rebuild the table, keeping every row and its seq."""
    (sql,) = con.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ).fetchone()
    if "'code'" in sql:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("ALTER TABLE events RENAME TO events_old")
        con.execute(_EVENTS_DDL)
        con.execute(
            "INSERT INTO events (seq, ts_ms, actor, type, object_id, klass, payload)"
            " SELECT seq, ts_ms, actor, type, object_id, klass, payload FROM events_old"
        )
        con.execute("DROP TABLE events_old")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def open_workspace_db(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    con.execute(_EVENTS_DDL)
    _migrate_event_actors(con)
    existing = {row[1] for row in con.execute("PRAGMA table_info(panels)")}
    for column, ddl in _PANEL_COLUMNS.items():
        if column not in existing:
            con.execute(ddl)
    have = {row[1] for row in con.execute("PRAGMA table_info(catalog_metrics)")}
    for column, ddl in _METRIC_COLUMNS.items():
        if column not in have:
            con.execute(ddl)
    con.execute(
        "CREATE INDEX IF NOT EXISTS catalog_metrics_family ON catalog_metrics (source, family)"
    )
    for table in _WORKSPACE_TABLES:
        if "workspace" not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(_WORKSPACE_COLUMN.format(table=table))
    for ddl in _WORKSPACE_INDEXES:
        con.execute(ddl)
    _seed_w1(con)
    return con
