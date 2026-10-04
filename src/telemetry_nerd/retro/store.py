"""Lessons and catalog proposals: global rows (not workspace-scoped), JSON per row."""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel

from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.retro.models import CatalogProposal, Lesson

_TABLES = {Lesson: ("lessons", "lesson"), CatalogProposal: ("catalog_proposals", "proposal")}


class RetroStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._db = con

    def _put(self, obj: Lesson | CatalogProposal) -> None:
        table, _ = _TABLES[type(obj)]
        self._db.execute(
            f"INSERT INTO {table} (id, workspace, data) VALUES (?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET data = excluded.data",
            (obj.id, obj.workspace, obj.model_dump_json()),
        )

    def _get[M: BaseModel](self, model: type[M], obj_id: str) -> M:
        table, kind = _TABLES[model]
        row = self._db.execute(f"SELECT data FROM {table} WHERE id = ?", (obj_id,)).fetchone()
        if row is None:
            raise NotFound(f"{kind} {obj_id} not found")
        return model.model_validate_json(row[0])

    def _all[M: BaseModel](self, model: type[M]) -> list[M]:
        table, _ = _TABLES[model]
        rows = self._db.execute(
            f"SELECT data FROM {table} ORDER BY CAST(ltrim(id, 'abcdefghijklmnopqrstuvwxyz') "
            "AS INTEGER)"
        )
        return [model.model_validate_json(r[0]) for r in rows]

    put_lesson = _put
    put_proposal = _put

    def lesson(self, lesson_id: str) -> Lesson:
        return self._get(Lesson, lesson_id)

    def proposal(self, proposal_id: str) -> CatalogProposal:
        return self._get(CatalogProposal, proposal_id)

    def lessons(self) -> list[Lesson]:
        return self._all(Lesson)

    def proposals(self) -> list[CatalogProposal]:
        return self._all(CatalogProposal)
