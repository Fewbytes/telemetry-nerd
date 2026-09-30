"""Workspace objects in SQLite. M1: id allocation and panels."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import now_ms

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
"""


@dataclass(frozen=True)
class Panel:
    id: str
    question: str
    status: str
    spec: dict
    dataset_ids: list[str]
    created_at_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


class WorkspaceStore:
    def __init__(self, path: str | Path, clock: Callable[[], int] = now_ms) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self._clock = clock

    def next_id(self, prefix: str) -> str:
        (n,) = self._db.execute(
            """INSERT INTO counters (prefix, n) VALUES (?, 1)
               ON CONFLICT (prefix) DO UPDATE SET n = n + 1 RETURNING n""",
            (prefix,),
        ).fetchone()
        return f"{prefix}{n}"

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
            "INSERT INTO panels VALUES (?, ?, ?, ?, ?, ?)",
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

    def get_panel(self, panel_id: str) -> Panel:
        row = self._db.execute("SELECT * FROM panels WHERE id = ?", (panel_id,)).fetchone()
        if row is None:
            raise NotFound(f"panel {panel_id} not found")
        return self._panel(row)

    def list_panels(self) -> list[Panel]:
        rows = self._db.execute(
            "SELECT * FROM panels ORDER BY created_at_ms DESC, CAST(substr(id, 2) AS INTEGER) DESC"
        ).fetchall()
        return [self._panel(r) for r in rows]

    @staticmethod
    def _panel(row: tuple) -> Panel:
        return Panel(row[0], row[1], row[2], json.loads(row[3]), json.loads(row[4]), row[5])
