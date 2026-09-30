"""Reasoning objects stored as JSON rows in one table. Soft deletes only."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.workspace.models import (
    Annotation,
    AnnotationIn,
    Finding,
    FindingIn,
    Gap,
    GapIn,
    Hypothesis,
    HypothesisStatus,
    Message,
    Thread,
    TimeSpan,
    Verdict,
)

M = TypeVar("M", bound=BaseModel)


class ObjectStore:
    def __init__(
        self,
        con: sqlite3.Connection,
        new_id: Callable[[str], str],
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._db = con
        self._new_id = new_id
        self._clock = clock

    # generic ------------------------------------------------------------
    def _insert(self, kind: str, obj: BaseModel, anchor: str | None) -> None:
        self._db.execute(
            "INSERT INTO objects (id, kind, anchor, deleted, created_at_ms, data) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (obj.id, kind, anchor, obj.created_at_ms, obj.model_dump_json()),
        )

    def _update(self, obj: BaseModel, deleted: bool = False) -> None:
        self._db.execute(
            "UPDATE objects SET data = ?, deleted = ? WHERE id = ?",
            (obj.model_dump_json(), int(deleted), obj.id),
        )

    def _get(self, kind: str, model: type[M], obj_id: str) -> M:
        row = self._db.execute(
            "SELECT data FROM objects WHERE id = ? AND kind = ?", (obj_id, kind)
        ).fetchone()
        if row is None:
            raise NotFound(f"{kind} {obj_id} not found")
        return model.model_validate_json(row[0])

    def _list(
        self, kind: str, model: type[M], anchor: str | None = None, include_deleted: bool = True
    ) -> list[M]:
        sql = "SELECT data FROM objects WHERE kind = ?"
        args: list = [kind]
        if anchor is not None:
            sql += " AND anchor = ?"
            args.append(anchor)
        if not include_deleted:
            sql += " AND deleted = 0"
        sql += " ORDER BY created_at_ms, CAST(substr(id, 2) AS INTEGER)"
        return [model.model_validate_json(r[0]) for r in self._db.execute(sql, args)]

    # annotations --------------------------------------------------------
    def create_annotation(self, data: AnnotationIn, author: str) -> Annotation:
        a = Annotation(
            **data.model_dump(), id=self._new_id("a"), author=author, created_at_ms=self._clock()
        )
        self._insert("annotation", a, a.panel)
        return a

    def get_annotation(self, obj_id: str) -> Annotation:
        return self._get("annotation", Annotation, obj_id)

    def list_annotations(
        self, panel: str | None = None, include_deleted: bool = False
    ) -> list[Annotation]:
        return self._list("annotation", Annotation, panel, include_deleted)

    def delete_annotation(self, obj_id: str) -> Annotation:
        a = self.get_annotation(obj_id).model_copy(update={"deleted": True})
        self._update(a, deleted=True)
        return a

    # hypotheses ---------------------------------------------------------
    def create_hypothesis(self, statement: str, author: str) -> Hypothesis:
        now = self._clock()
        h = Hypothesis(
            id=self._new_id("h"),
            statement=statement.strip(),
            author=author,
            created_at_ms=now,
            updated_at_ms=now,
        )
        self._insert("hypothesis", h, None)
        return h

    def get_hypothesis(self, obj_id: str) -> Hypothesis:
        return self._get("hypothesis", Hypothesis, obj_id)

    def set_hypothesis_status(
        self, obj_id: str, status: HypothesisStatus
    ) -> tuple[str, Hypothesis]:
        h = self.get_hypothesis(obj_id)
        updated = h.model_copy(update={"status": status, "updated_at_ms": self._clock()})
        updated = Hypothesis.model_validate(updated.model_dump())  # re-validate status
        self._update(updated)
        return h.status, updated

    def link_evidence(self, obj_id: str, finding_id: str, stance: str) -> Hypothesis:
        h = self.get_hypothesis(obj_id)
        field = "evidence_for" if stance == "for" else "evidence_against"
        ids = getattr(h, field)
        if finding_id in ids:
            return h
        updated = h.model_copy(update={field: [*ids, finding_id], "updated_at_ms": self._clock()})
        self._update(updated)
        return updated

    def list_hypotheses(self) -> list[Hypothesis]:
        return self._list("hypothesis", Hypothesis)

    # findings -----------------------------------------------------------
    def create_finding(self, data: FindingIn, author: str) -> Finding:
        f = Finding(
            **data.model_dump(), id=self._new_id("f"), author=author, created_at_ms=self._clock()
        )
        self._insert("finding", f, data.answers_panel)
        return f

    def get_finding(self, obj_id: str) -> Finding:
        return self._get("finding", Finding, obj_id)

    def set_verdict(self, obj_id: str, verdict: Verdict, comment: str | None) -> Finding:
        f = self.get_finding(obj_id)
        updated = Finding.model_validate(
            {**f.model_dump(), "verdict": verdict, "verdict_comment": comment}
        )
        self._update(updated)
        return updated

    def list_findings(self) -> list[Finding]:
        return self._list("finding", Finding)

    # gaps ---------------------------------------------------------------
    def create_gap(self, data: GapIn, author: str) -> Gap:
        g = Gap(
            **data.model_dump(), id=self._new_id("g"), author=author, created_at_ms=self._clock()
        )
        self._insert("gap", g, None)
        return g

    def list_gaps(self) -> list[Gap]:
        return self._list("gap", Gap)

    # threads ------------------------------------------------------------
    def create_thread(self, anchor: str | None, selection: TimeSpan | None, author: str) -> Thread:
        t = Thread(
            id=self._new_id("t"),
            anchor=anchor,
            selection=selection,
            author=author,
            created_at_ms=self._clock(),
        )
        self._insert("thread", t, anchor)
        return t

    def add_message(self, thread_id: str, text: str, author: str) -> Message:
        self._get("thread", Thread, thread_id)
        m = Message(
            id=self._new_id("m"),
            thread=thread_id,
            author=author,
            text=text.strip(),
            created_at_ms=self._clock(),
        )
        self._insert("message", m, thread_id)
        return m

    def get_thread(self, obj_id: str) -> Thread:
        t = self._get("thread", Thread, obj_id)
        return t.model_copy(update={"messages": self._list("message", Message, obj_id)})

    def list_threads(self, anchor: str | None = None) -> list[Thread]:
        return [self.get_thread(t.id) for t in self._list("thread", Thread, anchor)]
