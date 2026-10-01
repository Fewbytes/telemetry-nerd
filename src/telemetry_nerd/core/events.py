"""Append-only event log (spec §2.3) with live fan-out and channel claim cursors."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Literal, get_args

from telemetry_nerd.model.jsonsafe import finite
from telemetry_nerd.model.time import now_ms

log = logging.getLogger(__name__)

Actor = Literal["claude", "user", "system"]
Klass = Literal["intentional", "ambient", "internal"]
_ACTORS = frozenset(get_args(Actor))

INTENTIONAL_TYPES = frozenset(
    {
        "thread.message",
        "finding.verdict",
        "hypothesis.status_changed",
        "annotation.created",
        "catalog.claimed",  # a user's catalog edit is a correction Claude must know about
    }
)
AMBIENT_TYPES = frozenset(
    {"panel.created", "panel.closed", "focus.changed", "panel.y_view_selected"}
)


def check_actor(actor: str) -> None:
    if actor not in _ACTORS:
        raise ValueError(f"unknown actor {actor!r}; expected one of {sorted(_ACTORS)}")


def classify(actor: str, type: str, payload: dict | None = None) -> Klass:
    """Only human actions reach Claude; Claude's own actions never echo back."""
    if actor != "user":
        return "internal"
    if type == "object.highlighted":
        # a pin with a note is a message to Claude; a bare pin is just "I'm looking here"
        note = (payload or {}).get("note")
        return "intentional" if isinstance(note, str) and note.strip() else "ambient"
    if type in INTENTIONAL_TYPES:
        return "intentional"
    if type in AMBIENT_TYPES:
        return "ambient"
    return "internal"


@dataclass(frozen=True)
class Event:
    seq: int
    ts_ms: int
    actor: str
    type: str
    object_id: str | None
    klass: str
    payload: dict

    def to_dict(self) -> dict:
        return asdict(self)


_COLS = "seq, ts_ms, actor, type, object_id, klass, payload"


def _event(row: tuple) -> Event:
    return Event(row[0], row[1], row[2], row[3], row[4], row[5], json.loads(row[6]))


class EventLog:
    def __init__(self, con: sqlite3.Connection, clock: Callable[[], int] = now_ms) -> None:
        self._db = con
        self._clock = clock
        self._subscribers: set[asyncio.Queue] = set()
        self._depth = 0
        self._pending: list[dict] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run store writes and their events atomically; fan-out happens only after COMMIT.

        Re-entrant: nested calls join the outermost transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        self._pending = []
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            self._pending = []
            raise
        finally:
            self._depth = 0
        pending, self._pending = self._pending, []
        for event in pending:
            self._fan_out(event)

    def append(
        self, actor: str, type: str, object_id: str | None = None, payload: dict | None = None
    ) -> Event:
        check_actor(actor)
        body = finite(payload or {})
        klass = classify(actor, type, body)
        ts = self._clock()
        cur = self._db.execute(
            "INSERT INTO events (ts_ms, actor, type, object_id, klass, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, actor, type, object_id, klass, json.dumps(body)),
        )
        event = Event(cur.lastrowid, ts, actor, type, object_id, klass, body)
        if self._depth:
            self._pending.append(event.to_dict())
        else:
            self._fan_out(event.to_dict())
        return event

    def since(self, seq: int, limit: int = 1000) -> list[Event]:
        rows = self._db.execute(
            f"SELECT {_COLS} FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
        ).fetchall()
        return [_event(r) for r in rows]

    @property
    def last_seq(self) -> int:
        (seq,) = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return seq

    def message_seqs(self) -> dict[str, int]:
        """Message id -> seq of its `thread.message` event (delivery state in the UI)."""
        rows = self._db.execute(
            "SELECT json_extract(payload, '$.message'), seq FROM events "
            "WHERE type = 'thread.message'"
        ).fetchall()
        return {mid: seq for mid, seq in rows if mid is not None}

    # live fan-out -------------------------------------------------------
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _fan_out(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event subscriber queue full; dropping seq %s", event["seq"])

    # channel delivery ---------------------------------------------------
    def cursor(self, consumer: str) -> int:
        """Highest seq delivered to (acked by) `consumer`; 0 before the first delivery."""
        row = self._db.execute(
            "SELECT cursor FROM consumers WHERE name = ?", (consumer,)
        ).fetchone()
        return row[0] if row is not None else 0

    def seed_consumer(self, consumer: str, kind: str) -> None:
        """Give a first-seen consumer the kind's max cursor so it only sees new events.

        Per-session consumers (dtk) start with no row; without seeding, cursor 0 would
        redeliver the whole backlog to a session that was never there. Existing rows
        (and the plain-kind row) are left untouched.
        """
        self._db.execute(
            "INSERT INTO consumers (name, cursor) "
            "SELECT ?, COALESCE(MAX(cursor), 0) FROM consumers "
            "WHERE name = ? OR name LIKE ? "
            "ON CONFLICT (name) DO NOTHING",
            (consumer, kind, f"{kind}-%"),
        )

    def peek(self, consumer: str) -> tuple[list[Event], list[Event], int]:
        """The pending batch past the cursor, without advancing it.

        Returns (intentional, ambient, up_to). Ambient events ride along with the next
        intentional one: the batch ends at (and `up_to` is) the last intentional event,
        so ambient events after it stay pending and attach to the next one (spec §7.2).
        With no intentional event pending the batch is empty and `up_to` is the current
        cursor."""
        cursor = self.cursor(consumer)
        events = [
            _event(r)
            for r in self._db.execute(
                f"SELECT {_COLS} FROM events WHERE seq > ? AND klass != 'internal' ORDER BY seq",
                (cursor,),
            ).fetchall()
        ]
        intentional = [e for e in events if e.klass == "intentional"]
        if not intentional:
            return [], [], cursor
        # Ambient after the last intentional event stays pending: it attaches to the
        # NEXT intentional one (spec §7.2), so the cursor must not advance past it.
        up_to = intentional[-1].seq
        return intentional, [e for e in events if e.klass == "ambient" and e.seq < up_to], up_to

    def ack(self, consumer: str, up_to: int) -> None:
        """Mark everything up to `up_to` delivered; never moves the cursor back."""
        self._db.execute(
            "INSERT INTO consumers (name, cursor) VALUES (?, ?) "
            "ON CONFLICT (name) DO UPDATE SET cursor = MAX(cursor, excluded.cursor)",
            (consumer, up_to),
        )

    def claim(self, consumer: str) -> tuple[list[Event], list[Event]]:
        """Peek and ack in one step (hook delivery: printing is the send)."""
        intentional, ambient, up_to = self.peek(consumer)
        if intentional:
            self.ack(consumer, up_to)
        return intentional, ambient
