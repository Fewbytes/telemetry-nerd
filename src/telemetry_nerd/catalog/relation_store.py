"""SQLite persistence for relation and binding claims."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable

from telemetry_nerd.catalog.relations import (
    BindingClaim,
    RelationClaim,
    ResolvedBinding,
    ResolvedRelation,
    winner,
)

_REL_COLS = (
    "level, source, subject, kind, object, origin, confidence, retracted, params, basis, ts_ms"
)
_BIND_COLS = "level, source, kind, key, origin, confidence, retracted, roles, join_on, basis, ts_ms"


def _rel(r: tuple) -> RelationClaim:
    return RelationClaim(
        level=r[0], source=r[1], subject=r[2], kind=r[3], object=r[4], origin=r[5],
        confidence=r[6], retracted=bool(r[7]), params=json.loads(r[8]), basis=r[9], ts_ms=r[10],
    )  # fmt: skip


def _bind(r: tuple) -> BindingClaim:
    return BindingClaim(
        level=r[0], source=r[1], kind=r[2], key=r[3], origin=r[4], confidence=r[5],
        retracted=bool(r[6]), roles=json.loads(r[7]), join_on=json.loads(r[8]), basis=r[9],
        ts_ms=r[10],
    )  # fmt: skip


def _rel_row(c: RelationClaim) -> tuple:
    return (
        c.level, c.source, c.subject, c.kind, c.object, c.origin, c.confidence, int(c.retracted),
        json.dumps(c.params, sort_keys=True), c.basis, c.ts_ms,
    )  # fmt: skip


class RelationStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._db = con

    # relations ----------------------------------------------------------
    def put_relations(self, claims: Iterable[RelationClaim]) -> int:
        """Upsert claims; an identical claim keeps its timestamp. Returns rows written."""
        before = self._db.total_changes
        self._db.executemany(
            f"INSERT INTO catalog_relations ({_REL_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (level, source, subject, kind, object, origin) DO UPDATE SET "
            "confidence = excluded.confidence, retracted = excluded.retracted, "
            "params = excluded.params, basis = excluded.basis, ts_ms = excluded.ts_ms "
            "WHERE confidence IS NOT excluded.confidence OR retracted IS NOT excluded.retracted "
            "OR params IS NOT excluded.params OR basis IS NOT excluded.basis",
            [_rel_row(c) for c in claims],
        )
        return self._db.total_changes - before

    def _relation_claims(self, level: str, source: str) -> dict[tuple, list[RelationClaim]]:
        groups: dict[tuple, list[RelationClaim]] = defaultdict(list)
        for row in self._db.execute(
            f"SELECT {_REL_COLS} FROM catalog_relations WHERE level = ? AND source = ?",
            (level, source),
        ):
            c = _rel(row)
            groups[(c.subject, c.kind, c.object)].append(c)
        return groups

    def relations(
        self,
        level: str,
        source: str,
        *,
        metric: str | None = None,
        kind: str | None = None,
        include_retracted: bool = False,
    ) -> list[ResolvedRelation]:
        out = []
        for (subj, k, obj), claims in sorted(self._relation_claims(level, source).items()):
            if (metric and metric not in (subj, obj)) or (kind and k != kind):
                continue
            win = winner(claims)
            assert win is not None
            if win.retracted and not include_retracted:
                continue
            out.append(
                ResolvedRelation(
                    subject=subj,
                    kind=k,
                    object=obj,
                    winner=win,
                    contested=any(c.retracted != win.retracted for c in claims),
                    claims=claims,
                )
            )
        return out

    # bindings -----------------------------------------------------------
    def put_binding(self, c: BindingClaim) -> None:
        self._db.execute(
            f"INSERT INTO catalog_bindings ({_BIND_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (level, source, kind, key, origin) DO UPDATE SET "
            "confidence = excluded.confidence, retracted = excluded.retracted, "
            "roles = excluded.roles, join_on = excluded.join_on, basis = excluded.basis, "
            "ts_ms = excluded.ts_ms",
            (
                c.level, c.source, c.kind, c.key, c.origin, c.confidence, int(c.retracted),
                json.dumps(c.roles, sort_keys=True), json.dumps(c.join_on), c.basis, c.ts_ms,
            ),
        )  # fmt: skip

    def bindings(
        self,
        level: str,
        source: str,
        *,
        kind: str | None = None,
        key: str | None = None,
        include_retracted: bool = False,
    ) -> list[ResolvedBinding]:
        groups: dict[tuple, list[BindingClaim]] = defaultdict(list)
        for row in self._db.execute(
            f"SELECT {_BIND_COLS} FROM catalog_bindings WHERE level = ? AND source = ?",
            (level, source),
        ):
            c = _bind(row)
            groups[(c.kind, c.key)].append(c)
        out = []
        for (k, ky), claims in sorted(groups.items()):
            if (kind and k != kind) or (key and ky != key):
                continue
            win = winner(claims)
            assert win is not None
            if win.retracted and not include_retracted:
                continue
            contested = any(c.retracted != win.retracted or c.roles != win.roles for c in claims)
            out.append(
                ResolvedBinding(kind=k, key=ky, winner=win, contested=contested, claims=claims)
            )
        return out

    # gaps raised for unfilled roles -------------------------------------
    def binding_gap(self, level: str, source: str, kind: str, key: str, role: str) -> str | None:
        row = self._db.execute(
            "SELECT gap_id FROM catalog_binding_gaps "
            "WHERE level = ? AND source = ? AND kind = ? AND key = ? AND role = ?",
            (level, source, kind, key, role),
        ).fetchone()
        return row[0] if row else None

    def set_binding_gap(
        self, level: str, source: str, kind: str, key: str, role: str, gap_id: str
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO catalog_binding_gaps VALUES (?, ?, ?, ?, ?, ?)",
            (level, source, kind, key, role, gap_id),
        )
