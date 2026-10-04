"""The workspaces table: create, list, update, settings and recorded sources (spec D2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from telemetry_nerd.model.errors import NotFound, WrongWorkspace

_COLS = "id, title, question, created_at_ms, opened_at_ms, archived"

#: stored lengths; switch results and channel lines quote them in full
TITLE_MAX = 120
QUESTION_MAX = 500

# Open threads: live threads whose last message is the user's, newest first. The one
# definition behind the registry counts and WorkspaceService.open_threads.
_OPEN_THREADS = (
    "SELECT t.id FROM objects t WHERE t.workspace = ? AND t.kind = 'thread' AND t.deleted = 0"
    " AND (SELECT json_extract(m.data, '$.author') FROM objects m"
    " WHERE m.kind = 'message' AND m.anchor = t.id AND m.deleted = 0"
    " ORDER BY m.created_at_ms DESC, CAST(substr(m.id, 2) AS INTEGER) DESC, m.rowid DESC"
    " LIMIT 1) = 'user'"
    " ORDER BY t.created_at_ms DESC, CAST(substr(t.id, 2) AS INTEGER) DESC, t.rowid DESC"
)


def open_thread_ids(con: sqlite3.Connection, wid: str) -> list[str]:
    """Workspace `wid`'s open threads (live, the user spoke last), newest first."""
    return [r[0] for r in con.execute(_OPEN_THREADS, (wid,))]


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise ValueError("workspace title must not be blank")
    if len(title) > TITLE_MAX:
        raise ValueError(f"workspace title is {len(title)} characters; the limit is {TITLE_MAX}")
    return title


def _question(question: str | None) -> str | None:
    """None or blank: no question."""
    question = (question or "").strip()
    if len(question) > QUESTION_MAX:
        raise ValueError(
            f"workspace question is {len(question)} characters; the limit is {QUESTION_MAX}"
        )
    return question or None


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


def wrong_workspace(con: sqlite3.Connection, obj_id: str, wid: str) -> WrongWorkspace:
    """The refusal for updating `obj_id` (in workspace `wid`) from another workspace."""
    row = con.execute("SELECT title FROM workspaces WHERE id = ?", (wid,)).fetchone()
    return WrongWorkspace(obj_id, wid, row[0] if row is not None else None)


class WorkspaceRegistry:
    def __init__(
        self, con: sqlite3.Connection, new_id: Callable[[str], str], clock: Callable[[], int]
    ) -> None:
        self._con = con
        self._new_id = new_id
        self._clock = clock

    def create(self, title: str, question: str | None = None) -> WorkspaceInfo:
        title, question = _title(title), _question(question)
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
        """None leaves a field as it is; a blank question clears it."""
        self.get(wid)
        fields: dict = {}
        if title is not None:
            fields["title"] = _title(title)
        if question is not None:
            fields["question"] = _question(question)
        for column, value in fields.items():  # validated first: all or nothing
            self._con.execute(f"UPDATE workspaces SET {column} = ? WHERE id = ?", (value, wid))
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
        if name == "default":  # daemon-owned, never recorded
            return
        sources = self._json(wid, "sources")
        if sources.get(name) == spec:
            return
        sources[name] = spec  # last write wins: a reconfigured source updates the record
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
        # Derived, never stored (spec): open panels, live hypotheses/findings, and open
        # threads (open_thread_ids, in SQL so every workspace counts without a scope switch).
        (panels,) = self._con.execute(
            "SELECT COUNT(*) FROM panels WHERE workspace = ? AND closed = 0", (wid,)
        ).fetchone()
        counts = {"panels": panels}
        for key, kind in (("hypotheses", "hypothesis"), ("findings", "finding")):
            (counts[key],) = self._con.execute(
                "SELECT COUNT(*) FROM objects WHERE workspace = ? AND kind = ? AND deleted = 0",
                (wid, kind),
            ).fetchone()
        counts["open_threads"] = len(open_thread_ids(self._con, wid))
        (last,) = self._con.execute(
            "SELECT MAX(ts_ms) FROM events WHERE workspace = ?", (wid,)
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
