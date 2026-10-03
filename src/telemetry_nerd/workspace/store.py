"""Workspace objects in SQLite. M1: id allocation and panels."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.workspace.db import open_workspace_db

_PANEL_COLS = "id, question, status, spec, dataset_ids, created_at_ms, answered_by, closed"


@dataclass(frozen=True)
class Panel:
    id: str
    question: str
    status: str
    spec: dict
    dataset_ids: list[str]
    created_at_ms: int
    answered_by: str | None = None
    closed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class WorkspaceStore:
    def __init__(
        self, db: str | Path | sqlite3.Connection, clock: Callable[[], int] = now_ms
    ) -> None:
        self._db = db if isinstance(db, sqlite3.Connection) else open_workspace_db(db)
        self._clock = clock

    @property
    def connection(self) -> sqlite3.Connection:
        return self._db

    def next_id(self, prefix: str) -> str:
        (n,) = self._db.execute(
            """INSERT INTO counters (prefix, n) VALUES (?, 1)
               ON CONFLICT (prefix) DO UPDATE SET n = n + 1 RETURNING n""",
            (prefix,),
        ).fetchone()
        return f"{prefix}{n}"

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute(
            "SELECT value FROM workspace_settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO workspace_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def create_panel(self, question: str, spec: dict, dataset_ids: list[str]) -> Panel:
        if not question or not question.strip():
            raise ValueError("every panel must answer an explicit question")
        panel = Panel(
            id=self.next_id("p"),
            question=question.strip(),
            status="open",
            spec=spec,
            dataset_ids=list(dataset_ids),
            created_at_ms=self._clock(),
        )
        self._db.execute(
            "INSERT INTO panels (id, question, status, spec, dataset_ids, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                panel.id,
                panel.question,
                panel.status,
                json.dumps(spec),
                json.dumps(panel.dataset_ids),
                panel.created_at_ms,
            ),
        )
        return panel

    def set_spec(self, panel_id: str, spec: dict) -> Panel:
        self.get_panel(panel_id)
        self._db.execute("UPDATE panels SET spec = ? WHERE id = ?", (json.dumps(spec), panel_id))
        return self.get_panel(panel_id)

    def get_panel(self, panel_id: str) -> Panel:
        row = self._db.execute(
            f"SELECT {_PANEL_COLS} FROM panels WHERE id = ?", (panel_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"panel {panel_id} not found")
        return self._panel(row)

    def list_panels(self, include_closed: bool = False) -> list[Panel]:
        where = "" if include_closed else "WHERE closed = 0"
        rows = self._db.execute(
            f"SELECT {_PANEL_COLS} FROM panels {where} "
            "ORDER BY created_at_ms DESC, CAST(substr(id, 2) AS INTEGER) DESC"
        ).fetchall()
        return [self._panel(r) for r in rows]

    def set_answered(self, panel_id: str, finding_id: str) -> Panel:
        self.get_panel(panel_id)
        self._db.execute(
            "UPDATE panels SET status = 'answered', answered_by = ? WHERE id = ?",
            (finding_id, panel_id),
        )
        return self.get_panel(panel_id)

    def close_panel(self, panel_id: str) -> Panel:
        self.get_panel(panel_id)
        self._db.execute("UPDATE panels SET closed = 1 WHERE id = ?", (panel_id,))
        return self.get_panel(panel_id)

    @staticmethod
    def _panel(row: tuple) -> Panel:
        return Panel(
            row[0],
            row[1],
            row[2],
            json.loads(row[3]),
            json.loads(row[4]),
            row[5],
            row[6],
            bool(row[7]),
        )
