"""Compact, Claude-facing views over the catalog: search rows and family overview."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable

from telemetry_nerd.catalog.models import CatalogEntry

#: fields shown per row (with the origin that wins each)
KEY_FIELDS = ("type", "unit", "role", "bounds")
#: origins that have actually interpreted a metric (rule/metadata only restate its name/HELP)
INTERPRETING = frozenset({"pack", "claude", "user"})


def family_prefix(metric: str) -> str:
    """`node_cpu_seconds_total` -> `node_cpu`; `up` -> `up`."""
    parts = metric.split("_")
    return "_".join(parts[:2]) if len(parts) > 2 else metric


def reviewed(entry: CatalogEntry) -> bool:
    """Someone (pack, Claude, user) has said what this metric is for, and nothing disagrees."""
    role = entry.fields.get("role")
    return role is not None and role.origin in INTERPRETING and not entry.conflicts()


def row(entry: CatalogEntry, hot: bool) -> dict:
    out: dict = {"metric": entry.metric}
    for f in KEY_FIELDS:
        if f in entry.fields:
            out[f] = entry.fields[f].value
    out["origins"] = {f: entry.fields[f].origin for f in KEY_FIELDS if f in entry.fields}
    if conflicts := entry.conflicts():
        out["conflicts"] = sorted(conflicts)
    out["reviewed"] = reviewed(entry)
    if hot:
        out["hot"] = True
    return out


def search(
    entries: Iterable[CatalogEntry],
    hot: Collection[str],
    *,
    query: str | None = None,
    prefix: str | None = None,
    needs_review: bool = False,
    limit: int = 50,
) -> dict:
    q = query.lower() if query else None
    picked = []
    for e in entries:
        if prefix and not e.metric.startswith(prefix):
            continue
        if q:
            desc = e.fields["description"].value.lower() if "description" in e.fields else ""
            if q not in e.metric.lower() and q not in desc:
                continue
        if needs_review and reviewed(e):
            continue
        picked.append(e)
    picked.sort(key=lambda e: (e.metric not in hot, e.metric))
    shown = picked[:limit]
    return {
        "total": len(picked),
        "returned": len(shown),
        "results": [row(e, e.metric in hot) for e in shown],
    }


def overview(entries: Iterable[CatalogEntry], top: int = 30) -> list[dict]:
    """Metric families (by name prefix), the least-reviewed and largest first."""
    fam: dict[str, list[bool]] = defaultdict(list)
    for e in entries:
        fam[family_prefix(e.metric)].append(reviewed(e))
    rows = [{"family": f, "metrics": len(r), "reviewed": sum(r)} for f, r in fam.items()]
    rows.sort(key=lambda x: (-(x["metrics"] - x["reviewed"]), x["family"]))
    return rows[:top]
