"""SQLite persistence for T1 operating profiles (bead 2as.7).

One row per (source, profiled expression): the latest successful profile, or the latest failure
(kept so a failing source is not re-queried on every view)."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


def profile_id(source: str, expr: str) -> str:
    return "op-" + hashlib.sha256(f"{source}\0{expr}".encode()).hexdigest()[:12]


@dataclass(frozen=True)
class ProfileRow:
    id: str
    source: str
    expr: str
    status: str  # ok | failed
    computed_at_ms: int  # of the stored profile (ok) or of the failed attempt (failed)
    data: str | None  # profile JSON (the last good one survives a later failure)
    error: str | None
    failed_at_ms: int | None


_COLS = "id, source, expr, status, computed_at_ms, data, error, failed_at_ms"


class ProfileStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._db = con

    def get(self, source: str, expr: str) -> ProfileRow | None:
        row = self._db.execute(
            f"SELECT {_COLS} FROM operating_profiles WHERE source = ? AND expr = ?",
            (source, expr),
        ).fetchone()
        return ProfileRow(*row) if row else None

    def by_id(self, pid: str) -> ProfileRow | None:
        row = self._db.execute(
            f"SELECT {_COLS} FROM operating_profiles WHERE id = ?", (pid,)
        ).fetchone()
        return ProfileRow(*row) if row else None

    def put_ok(self, source: str, expr: str, computed_at_ms: int, data: str) -> str:
        pid = profile_id(source, expr)
        self._db.execute(
            f"INSERT INTO operating_profiles ({_COLS}) VALUES (?, ?, ?, 'ok', ?, ?, NULL, NULL) "
            "ON CONFLICT (source, expr) DO UPDATE SET status = 'ok', "
            "computed_at_ms = excluded.computed_at_ms, data = excluded.data, error = NULL, "
            "failed_at_ms = NULL",
            (pid, source, expr, computed_at_ms, data),
        )
        return pid

    def put_failed(self, source: str, expr: str, at_ms: int, error: str) -> None:
        """Record a failed attempt. A previous good profile keeps its data (still servable,
        marked stale by age); `failed_at_ms` is the attempt time."""
        pid = profile_id(source, expr)
        self._db.execute(
            "INSERT INTO operating_profiles (id, source, expr, status, computed_at_ms, data, "
            "error, failed_at_ms) VALUES (?, ?, ?, 'failed', ?, NULL, ?, ?) "
            "ON CONFLICT (source, expr) DO UPDATE SET status = 'failed', error = excluded.error, "
            "failed_at_ms = excluded.failed_at_ms",
            (pid, source, expr, at_ms, error, at_ms),
        )
