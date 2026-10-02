"""From extracted definitions and dashboard panels to proposed `context` claims (bead 2as.18). Pure.

`catalog_context` (WorkspaceService) validates input, reads the catalog and writes; the choosing of
what to claim for which metric lives here so it can be read and tested without a workspace."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from telemetry_nerd.catalog.context import GRAFANA_UNITS, SOURCE_CONFIDENCE, Extraction, PanelInfo
from telemetry_nerd.catalog.context_yaml import confidence_of
from telemetry_nerd.catalog.context_yaml import variants as name_variants
from telemetry_nerd.catalog.models import Claim, validate_value
from telemetry_nerd.charts.ycontext import counter_rate_metric, selector_parts


@dataclass
class Proposals:
    """Claims proposed per (metric, field); the more confident one wins, and a disagreement
    between two sources inside one call is reported, not hidden."""

    now_ms: int
    claims: dict[tuple[str, str], Claim] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    matched: set[str] = field(default_factory=set)
    via_pipeline: int = 0

    def propose(self, metric: str, field_: str, value: Any, confidence: float, cite: str) -> None:
        try:
            validate_value(field_, value)
        except ValueError:
            return
        key = (metric, field_)
        old = self.claims.get(key)
        if old is not None and old.value != value:
            kept = old if old.confidence >= confidence else None
            self.notes.append(
                f"{metric} {field_}: {old.citation} says {old.value!r}, {cite} says {value!r}; "
                f"kept {'the first' if kept else 'the second'}"
            )
            if kept:
                return
        elif old is not None and old.confidence >= confidence:
            return
        self.claims[key] = Claim(
            field=field_,  # type: ignore[arg-type]
            value=value,
            origin="context",
            confidence=confidence,
            citation=cite,
            ts_ms=self.now_ms,
        )


def propose_from_extraction(
    found: Extraction, has_metric: Callable[[str], bool], now_ms: int
) -> Proposals:
    """Match every definition (as registered, and as the pipeline's rules would leave it) and every
    single-metric dashboard panel against the metrics the source has."""
    out = Proposals(now_ms)
    for orig in found.definitions:
        tried = [
            (orig, orig.confidence),
            *[(v, confidence_of(v, True)) for v in name_variants(orig, found.transforms)],
        ]
        any_hit = False
        for d, confidence in tried:
            hits = [n for n in d.names if has_metric(n)]
            if d.kind in ("histogram", "summary") and has_metric(d.base):
                hits.append(d.base)  # a native histogram is exposed under its base name
            any_hit = any_hit or bool(hits)
            out.via_pipeline += bool(hits) and d is not orig
            for n in hits:
                out.matched.add(n)
                if d.help and d.help.strip():
                    out.propose(n, "description", d.help.strip(), confidence, d.citation)
                if d.unit:
                    out.propose(n, "unit", d.unit, confidence, d.citation)
                exact = n == d.base or n in d.names and d.kind in ("counter", "gauge")
                if (
                    d.kind in ("counter", "gauge")
                    and exact
                    or d.kind in ("histogram", "summary")
                    and n == d.base
                ):
                    out.propose(n, "type", d.kind, confidence, d.citation)
        if not any_hit:
            out.unmatched.append({"name": orig.base, "where": f"{orig.path}:{orig.line}"})
    for p in found.panels:
        _panel_claims(p, has_metric, out)
    return out


def _panel_claims(p: PanelInfo, has_metric: Callable[[str], bool], out: Proposals) -> None:
    """A dashboard panel speaks for a metric only when every expression is about that one metric:
    a plain selector or a rate of a counter. Its unit becomes that metric's unit."""
    metrics: set[str] = set()
    rate_forms: set[bool] = set()
    for expr in p.exprs:
        sel = selector_parts(expr)
        rate = counter_rate_metric(expr)
        m = sel[0] if sel else rate
        if m is None:
            return
        metrics.add(m)
        rate_forms.add(rate is not None and sel is None)
    if len(metrics) != 1 or len(rate_forms) != 1:
        return
    (metric,) = metrics
    (is_rate,) = rate_forms
    if not has_metric(metric):
        return
    cite = f'dashboard: {p.path} panel "{p.title}"'
    info = GRAFANA_UNITS.get(p.unit_id or "")
    if info is not None:
        unit, per_second = info
        if per_second == is_rate and (is_rate or not per_second):
            out.matched.add(metric)
            out.propose(
                metric,
                "unit",
                unit,
                SOURCE_CONFIDENCE["dashboard"],
                f"{cite} (grafana unit {p.unit_id!r})",
            )
    if p.description and p.description.strip() and not is_rate:
        out.matched.add(metric)
        out.propose(
            metric, "description", p.description.strip(), SOURCE_CONFIDENCE["dashboard"], cite
        )
