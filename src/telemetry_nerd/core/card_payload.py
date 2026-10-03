"""The metric card behind a panel (spec §6.4, bead 2as.12): what the catalog claims about the
metrics it shows, and how much to trust it. Pure functions over stored state."""

from __future__ import annotations

import polars as pl

from telemetry_nerd.catalog.models import CatalogEntry
from telemetry_nerd.catalog.search import KEY_FIELDS, reviewed

#: rows shown for every metric, claimed or not, so the user can fill a gap in place
EDITABLE = (
    "type", "unit", "bounds", "additivity_series", "additivity_time", "role", "description",
)  # fmt: skip
SHOWN = (*EDITABLE, "histogram_family", "thresholds", "statistic", "typical_range")
MAX_METRICS = 3


def field_rows(entry: CatalogEntry) -> list[dict]:
    conflicts = entry.conflicts()
    rows = []
    for f in SHOWN:
        claims = entry.claims.get(f, [])
        if not claims and f not in EDITABLE:
            continue
        win = entry.fields.get(f)
        rows.append(
            {
                "field": f,
                "editable": f in EDITABLE,
                "value": win.value if win else None,
                "origin": win.origin if win else None,
                "confidence": win.confidence if win else None,
                "basis": win.citation if win else None,
                "conflict": f in conflicts,
                "claims": [
                    {
                        "value": c.value,
                        "origin": c.origin,
                        "confidence": c.confidence,
                        "basis": c.citation,
                    }
                    for c in claims
                ],
            }
        )
    return rows


def browse_row(entry: CatalogEntry, findings: list[dict], verdict: str | None) -> dict:
    """One catalog-view row: the winners of the key fields with their provenance."""
    out: dict = {
        "metric": entry.metric, "present": entry.present, "is_family": entry.is_family,
        "family": entry.family, "dimension": entry.dimension, "family_members": entry.family_members,
    }  # fmt: skip
    for f in KEY_FIELDS:
        w = entry.fields.get(f)
        out[f] = w.value if w else None
    out["origins"] = {f: entry.fields[f].origin for f in KEY_FIELDS if f in entry.fields}
    out["confidences"] = {f: entry.fields[f].confidence for f in KEY_FIELDS if f in entry.fields}
    out["conflicts"] = sorted(entry.conflicts())
    out["findings"] = findings
    out["verdict"] = verdict
    out["reviewed"] = reviewed(entry)
    return out


def relation_row(r) -> dict:
    w = r.winner
    return {
        "subject": r.subject, "kind": r.kind, "object": r.object, "origin": w.origin,
        "confidence": w.confidence, "contested": r.contested, "basis": w.basis,
    }  # fmt: skip


def binding_row(b) -> dict:
    w = b.winner
    return {
        "kind": b.kind, "key": b.key, "roles": w.roles, "join_on": w.join_on, "origin": w.origin,
        "confidence": w.confidence, "contested": b.contested,
    }  # fmt: skip


def gap_pct(buckets, start_ms: int, end_ms: int, step_ms: int) -> float | None:
    """Share of expected buckets with no value, over all drawn series."""
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    n = df["series_id"].n_unique()
    expected = ((end_ms - start_ms) // step_ms + 1) * n
    if n == 0 or expected <= 0:
        return None
    have = df.filter(pl.col("avg").is_not_null() & pl.col("avg").is_not_nan()).height
    return round(max(0.0, 1.0 - have / expected), 4)


def profile_card(profile) -> dict:
    """Operating profile in a few numbers; the full thing is the `operating_profile` tool."""
    out = {
        "available": True,
        "window_ms": profile.window_ms,
        "step_ms": profile.step_ms,
        "kind": profile.kind,
        "stale": profile.stale,
        "computed_at_ms": profile.computed_at_ms,
        "series_total": profile.series_total,
        "range": profile.pooled.model_dump(exclude={"n"}),
        "caveats": profile.caveats,
    }
    seasonal = next((s.seasonal for s in profile.series if s.seasonal is not None), None)
    if seasonal is not None:
        out["seasonal"] = {"period": seasonal.period, "amplitude": seasonal.amplitude}
    return out
