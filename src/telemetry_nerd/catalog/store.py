"""SQLite persistence for catalog claims and the metric inventory."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Collection, Iterable

from telemetry_nerd.catalog.models import CatalogEntry, Claim, RelearnDiff, ordered
from telemetry_nerd.model.errors import NotFound


def _claim(row: tuple) -> Claim:
    return Claim(
        field=row[0],
        value=json.loads(row[1]),
        origin=row[2],
        confidence=row[3],
        verified_by=row[4],
        citation=row[5],
        ts_ms=row[6],
    )


_CLAIM_COLS = "field, value, origin, confidence, verified_by, citation, ts_ms"


class CatalogStore:
    def __init__(self, con: sqlite3.Connection) -> None:
        self._db = con

    # claims -------------------------------------------------------------
    def put_claim(self, source: str, metric: str, claim: Claim) -> None:
        """Insert or replace this origin's claim on the field; other origins are untouched."""
        self._db.execute(
            "INSERT INTO catalog_metrics (source, metric, first_seen_ms, last_seen_ms, present) "
            "VALUES (?, ?, ?, ?, 1) ON CONFLICT (source, metric) DO NOTHING",
            (source, metric, claim.ts_ms, claim.ts_ms),
        )
        self._db.execute(
            "INSERT INTO catalog_claims "
            "(source, metric, field, origin, value, confidence, verified_by, citation, ts_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, metric, field, origin) DO UPDATE SET "
            "value = excluded.value, confidence = excluded.confidence, "
            "verified_by = excluded.verified_by, citation = excluded.citation, "
            "ts_ms = excluded.ts_ms",
            (
                source,
                metric,
                claim.field,
                claim.origin,
                json.dumps(claim.value),
                claim.confidence,
                claim.verified_by,
                claim.citation,
                claim.ts_ms,
            ),
        )

    def put_claims_bulk(self, source: str, rows: Iterable[tuple[str, Claim]]) -> int:
        """Upsert many claims; a claim whose value, confidence and citation are unchanged is left
        alone (its timestamp stays), so re-learning an unchanged source writes nothing.
        Metrics must already be in the inventory (see `relearn`). Returns rows inserted/changed."""
        before = self._db.total_changes
        self._db.executemany(
            "INSERT INTO catalog_claims "
            "(source, metric, field, origin, value, confidence, verified_by, citation, ts_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, metric, field, origin) DO UPDATE SET "
            "value = excluded.value, confidence = excluded.confidence, "
            "verified_by = excluded.verified_by, citation = excluded.citation, "
            "ts_ms = excluded.ts_ms "
            "WHERE value IS NOT excluded.value OR confidence IS NOT excluded.confidence "
            "OR citation IS NOT excluded.citation",
            [
                (
                    source,
                    metric,
                    c.field,
                    c.origin,
                    json.dumps(c.value),
                    c.confidence,
                    c.verified_by,
                    c.citation,
                    c.ts_ms,
                )
                for metric, c in rows
            ],
        )
        return self._db.total_changes - before

    def names(self, source: str, prefix: str | None = None, limit: int = 1000) -> list[str]:
        """Present metric names (optionally under a prefix), alphabetical, without loading claims."""
        like = (prefix or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._db.execute(
            "SELECT metric FROM catalog_metrics WHERE source = ? AND present = 1 "
            "AND metric LIKE ? ESCAPE '\\' ORDER BY metric LIMIT ?",
            (source, like, limit),
        ).fetchall()
        return [r[0] for r in rows]

    @property
    def connection(self) -> sqlite3.Connection:
        return self._db

    def has_metric(self, source: str, metric: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM catalog_metrics WHERE source = ? AND metric = ?", (source, metric)
            ).fetchone()
            is not None
        )

    def claims_for(self, source: str, metric: str) -> list[Claim]:
        rows = self._db.execute(
            f"SELECT {_CLAIM_COLS} FROM catalog_claims WHERE source = ? AND metric = ?",
            (source, metric),
        ).fetchall()
        return [_claim(r) for r in rows]

    def _entry(self, source: str, metric: str, meta: tuple, claims: list[Claim]) -> CatalogEntry:
        by_field: dict[str, list[Claim]] = defaultdict(list)
        for c in claims:
            by_field[c.field].append(c)
        ranked = {f: ordered(cs) for f, cs in by_field.items()}
        return CatalogEntry(
            source=source,
            metric=metric,
            present=bool(meta[2]),
            first_seen_ms=meta[0],
            last_seen_ms=meta[1],
            fields={f: cs[0] for f, cs in ranked.items()},
            claims=ranked,
        )

    def entry(self, source: str, metric: str) -> CatalogEntry:
        meta = self._db.execute(
            "SELECT first_seen_ms, last_seen_ms, present FROM catalog_metrics "
            "WHERE source = ? AND metric = ?",
            (source, metric),
        ).fetchone()
        if meta is None:
            raise NotFound(f"no catalog entry for {metric!r} on source {source!r}")
        rows = self._db.execute(
            f"SELECT {_CLAIM_COLS} FROM catalog_claims WHERE source = ? AND metric = ?",
            (source, metric),
        ).fetchall()
        return self._entry(source, metric, meta, [_claim(r) for r in rows])

    def list_entries(self, source: str, *, present_only: bool = True) -> list[CatalogEntry]:
        metas = self._db.execute(
            "SELECT metric, first_seen_ms, last_seen_ms, present FROM catalog_metrics "
            "WHERE source = ? ORDER BY metric",
            (source,),
        ).fetchall()
        claims: dict[str, list[Claim]] = defaultdict(list)
        for row in self._db.execute(
            f"SELECT metric, {_CLAIM_COLS} FROM catalog_claims WHERE source = ?", (source,)
        ):
            claims[row[0]].append(_claim(row[1:]))
        return [
            self._entry(source, m[0], m[1:], claims[m[0]])
            for m in metas
            if m[3] or not present_only
        ]

    # inventory ----------------------------------------------------------
    def relearn(
        self, source: str, names: Collection[str], now_ms: int, *, complete: bool = True
    ) -> RelearnDiff:
        """Reconcile the metric inventory with a fresh listing.

        `complete=False` (a truncated/partial discovery) never marks metrics removed:
        absence from a partial listing proves nothing."""
        seen = set(names)
        known = {
            m: bool(p)
            for m, p in self._db.execute(
                "SELECT metric, present FROM catalog_metrics WHERE source = ?", (source,)
            )
        }
        new = sorted(seen - known.keys())
        returned = sorted(m for m in seen if known.get(m) is False)
        removed = sorted(m for m, p in known.items() if p and m not in seen) if complete else []
        self._db.executemany(
            "INSERT INTO catalog_metrics (source, metric, first_seen_ms, last_seen_ms, present) "
            "VALUES (?, ?, ?, ?, 1)",
            [(source, m, now_ms, now_ms) for m in new],
        )
        self._db.executemany(
            "UPDATE catalog_metrics SET present = 1, last_seen_ms = ? WHERE source = ? AND metric = ?",
            [(now_ms, source, m) for m in seen & known.keys()],
        )
        self._db.executemany(
            "UPDATE catalog_metrics SET present = 0 WHERE source = ? AND metric = ?",
            [(source, m) for m in removed],
        )
        return RelearnDiff(new=new, removed=removed, returned=returned)
