"""The workspaces table: create, list, update, settings and recorded sources (spec D2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from telemetry_nerd.model.errors import NotFound

_COLS = "id, title, question, created_at_ms, opened_at_ms, archived"


@dataclass(frozen=True)
class WorkspaceInfo:
    id: str
    title: str
    question: str | None
    created_at_ms: int
    opened_at_ms: int
    archived: bool
    last_activity_ms: int
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class WorkspaceRegistry:
    def __init__(
        self, con: sqlite3.Connection, new_id: Callable[[str], str], clock: Callable[[], int]
    ) -> None:
        self._con = con
        self._new_id = new_id
        self._clock = clock

    def create(self, title: str, question: str | None = None) -> WorkspaceInfo:
        title = title.strip()
        if not title:
            raise ValueError("workspace title must not be blank")
        wid = self._new_id("w")
        now = self._clock()
        self._con.execute(
            "INSERT INTO workspaces (id, title, question, created_at_ms, opened_at_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (wid, title, question, now, now),
        )
        return self.get(wid)

    def get(self, wid: str) -> WorkspaceInfo:
        row = self._con.execute(f"SELECT {_COLS} FROM workspaces WHERE id = ?", (wid,)).fetchone()
        if row is None:
            raise NotFound(f"workspace {wid} not found")
        return self._info(row)

    def list(self, include_archived: bool = False) -> list[WorkspaceInfo]:
        where = "" if include_archived else " WHERE archived = 0"
        rows = self._con.execute(f"SELECT {_COLS} FROM workspaces{where}").fetchall()
        infos = [self._info(r) for r in rows]
        infos.sort(key=lambda i: max(i.last_activity_ms, i.opened_at_ms), reverse=True)
        return infos

    def ids(self) -> list[str]:
        return [r[0] for r in self._con.execute("SELECT id FROM workspaces ORDER BY rowid")]

    def update(
        self,
        wid: str,
        *,
        title: str | None = None,
        question: str | None = None,
        archived: bool | None = None,
    ) -> WorkspaceInfo:
        self.get(wid)
        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError("workspace title must not be blank")
            self._con.execute("UPDATE workspaces SET title = ? WHERE id = ?", (title, wid))
        if question is not None:
            self._con.execute("UPDATE workspaces SET question = ? WHERE id = ?", (question, wid))
        if archived is not None:
            self._con.execute(
                "UPDATE workspaces SET archived = ? WHERE id = ?", (int(archived), wid)
            )
        return self.get(wid)

    def mark_opened(self, wid: str) -> None:
        """Make `wid` the active one: strictly newer than every other opened_at_ms, even
        when the clock has not advanced."""
        self.get(wid)
        (top,) = self._con.execute("SELECT MAX(opened_at_ms) FROM workspaces").fetchone()
        self._con.execute(
            "UPDATE workspaces SET opened_at_ms = ? WHERE id = ?",
            (max(self._clock(), top + 1), wid),
        )

    def active_id(self) -> str:
        row = self._con.execute(
            "SELECT id FROM workspaces ORDER BY opened_at_ms DESC, rowid LIMIT 1"
        ).fetchone()
        if row is None:
            raise NotFound("no workspaces")
        return row[0]

    def get_setting(self, wid: str, key: str, default: Any = None) -> Any:
        return self._json(wid, "settings").get(key, default)

    def set_setting(self, wid: str, key: str, value: Any) -> None:
        settings = self._json(wid, "settings")
        settings[key] = value
        self._write_json(wid, "settings", settings)

    def sources(self, wid: str) -> dict[str, dict]:
        return self._json(wid, "sources")

    def note_source(self, wid: str, name: str, spec: dict) -> None:
        sources = self._json(wid, "sources")
        if name in sources:
            return
        sources[name] = spec
        self._write_json(wid, "sources", sources)

    # internals ----------------------------------------------------------
    def _json(self, wid: str, column: str) -> dict:
        row = self._con.execute(f"SELECT {column} FROM workspaces WHERE id = ?", (wid,)).fetchone()
        if row is None:
            raise NotFound(f"workspace {wid} not found")
        return json.loads(row[0])

    def _write_json(self, wid: str, column: str, value: dict) -> None:
        self._con.execute(
            f"UPDATE workspaces SET {column} = ? WHERE id = ?", (json.dumps(value), wid)
        )

    def _info(self, row: tuple) -> WorkspaceInfo:
        wid = row[0]
        counts: dict[str, int] = {}
        (panels,) = self._con.execute(
            "SELECT COUNT(*) FROM panels WHERE workspace = ?", (wid,)
        ).fetchone()
        if panels:
            counts["panel"] = panels
        for kind, n in self._con.execute(
            "SELECT kind, COUNT(*) FROM objects WHERE workspace = ? AND deleted = 0 GROUP BY kind",
            (wid,),
        ):
            counts[kind] = n
        (last,) = self._con.execute(
            "SELECT MAX(t) FROM (SELECT MAX(ts_ms) AS t FROM events WHERE workspace = ?"
            " UNION ALL SELECT MAX(created_at_ms) FROM panels WHERE workspace = ?"
            " UNION ALL SELECT MAX(created_at_ms) FROM objects WHERE workspace = ?)",
            (wid, wid, wid),
        ).fetchone()
        return WorkspaceInfo(
            id=wid,
            title=row[1],
            question=row[2],
            created_at_ms=row[3],
            opened_at_ms=row[4],
            archived=bool(row[5]),
            last_activity_ms=last or 0,
            counts=counts,
        )
