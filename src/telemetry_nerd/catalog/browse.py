"""Paginated catalog queries for the catalog view (bead 2as.13), done in SQL.

A catalog can hold 100k metrics, so filtering and paging never load claims outside the page.
"Winning" claims use the Python resolver's order (origin rank, confidence, recency), so a filter on
origin or confidence is about what the catalog shows, not about any claim that merely exists.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from telemetry_nerd.catalog.models import ORIGIN_RANK
from telemetry_nerd.catalog.search import KEY_FIELDS

SORTS = ("name", "weakest", "conflicts")
MAX_PAGE = 100
_RANK = " ".join(f"WHEN '{o}' THEN {r}" for o, r in ORIGIN_RANK.items())
_KEYS = ",".join(f"'{f}'" for f in KEY_FIELDS)

# winners per (metric, field); metrics whose claims disagree (prose never conflicts); metrics a
# pack/Claude/user has interpreted (role); metrics with a finding filed by a scan
_CTE = f"""
WITH ranked AS (
  SELECT metric, field, origin, confidence, ROW_NUMBER() OVER (
    PARTITION BY metric, field
    ORDER BY CASE origin {_RANK} ELSE 0 END DESC, confidence DESC, ts_ms DESC, value DESC
  ) AS rn FROM catalog_claims WHERE source = :source),
win AS (SELECT metric, field, origin, confidence FROM ranked WHERE rn = 1),
keyed AS (SELECT * FROM win WHERE field IN ({_KEYS})),
disagree AS (
  SELECT metric, COUNT(*) AS n FROM (
    SELECT metric, field FROM catalog_claims
    WHERE source = :source AND field != 'description'
    GROUP BY metric, field HAVING COUNT(DISTINCT value) > 1)
  GROUP BY metric),
interp AS (SELECT metric FROM win WHERE field = 'role' AND origin IN ('pack', 'claude', 'user')),
found AS (SELECT DISTINCT metric FROM catalog_findings WHERE source = :source)
"""


def like(text: str) -> str:
    """Escape LIKE wildcards (ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class Browse:
    q: str | None = None
    prefix: str | None = None
    origin: str | None = None
    max_confidence: float | None = None
    conflicts: bool = False
    findings: bool = False
    reviewed: bool | None = None
    removed: bool = False
    #: list family members too (default: a family stands for its members)
    members: bool = False
    #: only the members of this family
    family: str | None = None
    sort: str = "name"
    offset: int = 0
    limit: int = 50


def browse(
    con: sqlite3.Connection, source: str, b: Browse
) -> tuple[int, list[str], dict[str, int]]:
    """(total matching, metric names on the page, summary counts for the whole source)."""
    if b.sort not in SORTS:
        raise ValueError(f"unknown sort {b.sort!r}; expected one of {list(SORTS)}")
    if b.origin is not None and b.origin not in ORIGIN_RANK:
        raise ValueError(f"unknown origin {b.origin!r}; expected one of {sorted(ORIGIN_RANK)}")
    limit = max(1, min(b.limit, MAX_PAGE))
    params: dict[str, object] = {"source": source}
    conds = ["m.source = :source"] + ([] if b.removed else ["m.present = 1"])
    if b.family:
        conds.append("m.family = :family")
        params["family"] = b.family
    elif not b.members:
        conds.append("m.family IS NULL")
    if b.prefix:
        conds.append("m.metric LIKE :prefix ESCAPE '\\'")
        params["prefix"] = like(b.prefix) + "%"
    if b.q:
        conds.append(
            "(m.metric LIKE :q ESCAPE '\\' OR EXISTS (SELECT 1 FROM catalog_claims d "
            "WHERE d.source = :source AND d.metric = m.metric AND d.field = 'description' "
            "AND d.value LIKE :q ESCAPE '\\'))"
        )
        params["q"] = "%" + like(b.q) + "%"
    if b.origin:
        conds.append(
            "EXISTS (SELECT 1 FROM keyed k WHERE k.metric = m.metric AND k.origin = :origin)"
        )
        params["origin"] = b.origin
    if b.max_confidence is not None:
        conds.append(
            "EXISTS (SELECT 1 FROM keyed k WHERE k.metric = m.metric AND k.confidence < :maxc)"
        )
        params["maxc"] = b.max_confidence
    if b.conflicts:
        conds.append("m.metric IN (SELECT metric FROM disagree)")
    if b.findings:
        conds.append("m.metric IN (SELECT metric FROM found)")
    done = (
        "m.metric IN (SELECT metric FROM interp) AND m.metric NOT IN (SELECT metric FROM disagree)"
    )
    if b.reviewed is True:
        conds.append(done)
    elif b.reviewed is False:
        conds.append(f"NOT ({done})")
    where = " AND ".join(conds)
    order = {
        "name": "m.metric",
        "weakest": "COALESCE((SELECT MIN(confidence) FROM keyed k WHERE k.metric = m.metric), -1), m.metric",
        "conflicts": "COALESCE((SELECT n FROM disagree d WHERE d.metric = m.metric), 0) DESC, m.metric",
    }[b.sort]
    total = con.execute(
        f"{_CTE} SELECT COUNT(*) FROM catalog_metrics m WHERE {where}", params
    ).fetchone()[0]
    page = [
        r[0]
        for r in con.execute(
            f"{_CTE} SELECT m.metric FROM catalog_metrics m WHERE {where} "
            f"ORDER BY {order} LIMIT :limit OFFSET :offset",
            {**params, "limit": limit, "offset": max(0, b.offset)},
        )
    ]
    live = "m.source = :source AND m.present = 1 AND m.family IS NULL"
    counts = con.execute(
        f"""{_CTE} SELECT
          COUNT(*),
          SUM(CASE WHEN {done} THEN 1 ELSE 0 END),
          SUM(CASE WHEN m.metric IN (SELECT metric FROM disagree) THEN 1 ELSE 0 END),
          SUM(CASE WHEN m.metric IN (SELECT metric FROM found) THEN 1 ELSE 0 END),
          SUM(m.is_family)
        FROM catalog_metrics m WHERE {live}""",
        {"source": source},
    ).fetchone()
    summary = dict(
        zip(
            ("metrics", "reviewed", "conflicts", "findings", "families"),
            (c or 0 for c in counts),
            strict=True,
        )
    )
    summary["family_members"] = con.execute(
        "SELECT COUNT(*) FROM catalog_metrics WHERE source = ? AND present = 1 AND family IS NOT NULL",
        (source,),
    ).fetchone()[0]
    return total, page, summary
