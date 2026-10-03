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

    def _family_of(self, source: str, metric: str) -> str | None:
        r = self._db.execute(
            "SELECT family FROM catalog_metrics WHERE source = ? AND metric = ?", (source, metric)
        ).fetchone()
        return r[0] if r else None

    def claims_for(self, source: str, metric: str) -> list[Claim]:
        """The metric's claims; a family member with none of its own inherits its family's."""
        rows = self._db.execute(
            f"SELECT {_CLAIM_COLS} FROM catalog_claims WHERE source = ? AND metric = ?",
            (source, metric),
        ).fetchall()
        if not rows and (fam := self._family_of(source, metric)):
            rows = self._db.execute(
                f"SELECT {_CLAIM_COLS} FROM catalog_claims WHERE source = ? AND metric = ?",
                (source, fam),
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
            "SELECT first_seen_ms, last_seen_ms, present, family, dimension, is_family "
            "FROM catalog_metrics WHERE source = ? AND metric = ?",
            (source, metric),
        ).fetchone()
        if meta is None:
            raise NotFound(f"no catalog entry for {metric!r} on source {source!r}")
        claims = self.claims_for(source, metric)
        entry = self._entry(source, metric, meta[:3], claims)
        entry.family, entry.dimension, entry.is_family = meta[3], meta[4], bool(meta[5])
        if entry.is_family:
            entry.family_members = self._db.execute(
                "SELECT COUNT(*) FROM catalog_metrics WHERE source = ? AND family = ?",
                (source, metric),
            ).fetchone()[0]
        elif (
            entry.family
            and claims
            and not any(
                True
                for _ in self._db.execute(
                    "SELECT 1 FROM catalog_claims WHERE source = ? AND metric = ? LIMIT 1",
                    (source, metric),
                )
            )
        ):
            entry.inherited_from = entry.family
        return entry

    def list_entries(
        self, source: str, *, present_only: bool = True, include_members: bool = False
    ) -> list[CatalogEntry]:
        """Entries without family members by default: a family stands for its members."""
        metas = self._db.execute(
            "SELECT metric, first_seen_ms, last_seen_ms, present FROM catalog_metrics "
            f"WHERE source = ? {'' if include_members else 'AND family IS NULL'} ORDER BY metric",
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
                "SELECT metric, present FROM catalog_metrics WHERE source = ? AND is_family = 0",
                (source,),
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


class FamilyStore:
    """Name-template families (bead 2as.16): a family is a pseudo-metric named by its template
    whose members are metrics whose names encode a dimension."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._db = con

    def rejected(self, source: str) -> set[str]:
        return {r[0] for r in self._db.execute(
            "SELECT template FROM catalog_family_rejections WHERE source = ?", (source,)
        )}  # fmt: skip

    def confirmed(self, source: str) -> set[str]:
        return {r[0] for r in self._db.execute(
            "SELECT template FROM catalog_families WHERE source = ? AND status = 'confirmed'", (source,)
        )}  # fmt: skip

    def apply(self, source: str, detection, now_ms: int) -> dict[str, int]:
        """Record detected families and memberships. Rejected templates are ignored; confirmed
        families keep their status. Members no longer in a detected family are released, except
        those of confirmed families (a decision outlives a changed listing)."""
        rejected = self.rejected(source)
        confirmed = self.confirmed(source)
        fams = [f for f in detection.families if f.template not in rejected]
        keep = {f.template for f in fams}
        assign = {n: ta for n, ta in detection.assignment.items() if ta[0] in keep}
        con = self._db
        # release members of families that are no longer detected (unless confirmed)
        old = {r[0] for r in con.execute(
            "SELECT template FROM catalog_families WHERE source = ?", (source,)
        )}  # fmt: skip
        gone = [t for t in old - keep if t not in confirmed]
        for t in gone:
            con.execute(
                "UPDATE catalog_metrics SET family = NULL, dimension = NULL WHERE source = ? AND family = ?",
                (source, t),
            )
            con.execute(
                "DELETE FROM catalog_metrics WHERE source = ? AND metric = ? AND is_family = 1",
                (source, t),
            )
            con.execute("DELETE FROM catalog_claims WHERE source = ? AND metric = ?", (source, t))
            con.execute(
                "DELETE FROM catalog_families WHERE source = ? AND template = ?", (source, t)
            )
        for f in fams:
            con.execute(
                "INSERT INTO catalog_metrics (source, metric, first_seen_ms, last_seen_ms, present, is_family) "
                "VALUES (?, ?, ?, ?, 1, 1) ON CONFLICT (source, metric) DO UPDATE SET "
                "last_seen_ms = excluded.last_seen_ms, present = 1, is_family = 1",
                (source, f.template, now_ms, now_ms),
            )
            con.execute(
                "INSERT INTO catalog_families (source, template, members, distinct_dims, status, ts_ms) "
                "VALUES (?, ?, ?, ?, 'detected', ?) ON CONFLICT (source, template) DO UPDATE SET "
                "members = excluded.members, distinct_dims = excluded.distinct_dims, ts_ms = excluded.ts_ms",
                (source, f.template, f.members, f.distinct, now_ms),
            )
        # everything detected this round: reset then assign (family rows of confirmed families stay)
        con.execute(
            "UPDATE catalog_metrics SET family = NULL, dimension = NULL "
            "WHERE source = ? AND family IS NOT NULL AND family IN "
            "(SELECT template FROM catalog_families WHERE source = ? AND status != 'confirmed')",
            (source, source),
        )
        con.executemany(
            "UPDATE catalog_metrics SET family = ?, dimension = ? WHERE source = ? AND metric = ?",
            [(t, d, source, n) for n, (t, d) in assign.items()],
        )
        return {
            "families": len(fams),
            "members": len(assign),
            "rejected": len(rejected & {f.template for f in detection.families}),
        }

    def list(self, source: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT template, members, distinct_dims, status, decided_by FROM catalog_families "
            "WHERE source = ? ORDER BY (status = 'confirmed'), members DESC, template",
            (source,),
        ).fetchall()
        return [
            {"template": t, "members": m, "distinct": d, "status": st, "decided_by": by}
            for t, m, d, st, by in rows
        ]

    def info(self, source: str, template: str) -> dict | None:
        r = self._db.execute(
            "SELECT members, distinct_dims, status, decided_by, basis FROM catalog_families "
            "WHERE source = ? AND template = ?",
            (source, template),
        ).fetchone()
        return None if r is None else {
            "template": template, "members": r[0], "distinct": r[1], "status": r[2],
            "decided_by": r[3], "basis": r[4],
        }  # fmt: skip

    def confirm(
        self, source: str, template: str, decided_by: str, basis: str | None, now_ms: int
    ) -> None:
        self._db.execute(
            "UPDATE catalog_families SET status = 'confirmed', decided_by = ?, basis = ?, ts_ms = ? "
            "WHERE source = ? AND template = ?",
            (decided_by, basis, now_ms, source, template),
        )

    def split(self, source: str, template: str, decided_by: str, now_ms: int) -> int:
        """Dissolve a family for good: members become ordinary metrics and the template is never
        proposed again. Returns the number of members released."""
        n = self._db.execute(
            "SELECT COUNT(*) FROM catalog_metrics WHERE source = ? AND family = ?",
            (source, template),
        ).fetchone()[0]
        self._db.execute(
            "UPDATE catalog_metrics SET family = NULL, dimension = NULL WHERE source = ? AND family = ?",
            (source, template),
        )
        self._db.execute(
            "DELETE FROM catalog_metrics WHERE source = ? AND metric = ? AND is_family = 1",
            (source, template),
        )
        self._db.execute(
            "DELETE FROM catalog_claims WHERE source = ? AND metric = ?", (source, template)
        )
        self._db.execute(
            "DELETE FROM catalog_families WHERE source = ? AND template = ?", (source, template)
        )
        self._db.execute(
            "INSERT OR REPLACE INTO catalog_family_rejections VALUES (?, ?, ?, ?)",
            (source, template, decided_by, now_ms),
        )
        return n

    def members(
        self, source: str, template: str, offset: int = 0, limit: int = 50
    ) -> list[tuple[str, str]]:
        return [tuple(r) for r in self._db.execute(
            "SELECT metric, dimension FROM catalog_metrics WHERE source = ? AND family = ? "
            "ORDER BY metric LIMIT ? OFFSET ?",
            (source, template, limit, offset),
        )]  # fmt: skip
