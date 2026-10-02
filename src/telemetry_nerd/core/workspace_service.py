"""Reasoning operations. Every mutation appends exactly one event (documented exceptions)."""

from __future__ import annotations

import functools
import inspect
import json
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from telemetry_nerd.analysis.samples import SampleStats
from telemetry_nerd.catalog.browse import Browse
from telemetry_nerd.catalog.browse import browse as browse_catalog
from telemetry_nerd.catalog.families import detect as detect_families
from telemetry_nerd.catalog.models import (
    ORIGIN_RANK,
    CatalogEntry,
    Claim,
    FieldName,
    Origin,
    RelearnDiff,
    resolve,
    validate_value,
)
from telemetry_nerd.catalog.packs import PackIndex, builtin_packs
from telemetry_nerd.catalog.relation_store import RelationStore
from telemetry_nerd.catalog.relations import (
    CORRELATED_MAX_CONFIDENCE,
    SUGGESTIONS,
    BindingClaim,
    Level,
    RelationClaim,
    ResolvedBinding,
    ResolvedRelation,
    canonical_ends,
    metric_slug,
    validate_binding,
    validate_relation,
)
from telemetry_nerd.catalog.rules import (
    Facts,
    derive_claims,
    facts_from_claims,
    facts_from_name,
    normalize_unit,
)
from telemetry_nerd.catalog.sample_store import SampleObservation, SampleStore
from telemetry_nerd.catalog.search import overview as family_overview
from telemetry_nerd.catalog.search import search as search_entries
from telemetry_nerd.catalog.store import CatalogStore, FamilyStore
from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.charts.dataview import SignalViews
from telemetry_nerd.charts.indexed import check_index, shifted
from telemetry_nerd.charts.spec import ChartSpec, Marginal, Reference, YContext
from telemetry_nerd.charts.units import infer_unit_with_provenance, metric_names
from telemetry_nerd.charts.yview import (
    BUILTIN_LABELS,
    INDEX_LABELS,
    MAX_SUGGESTIONS,
    YView,
    check_view,
    value_stats,
)
from telemetry_nerd.core.card_payload import binding_row, browse_row, field_rows, relation_row
from telemetry_nerd.core.coverage_check import claim_coverage
from telemetry_nerd.core.events import Actor, Event, EventLog, check_actor
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.companions import dataset_bundle
from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import format_duration, now_ms
from telemetry_nerd.workspace.models import (
    Annotation,
    AnnotationIn,
    AnnotationRef,
    Finding,
    FindingIn,
    Gap,
    GapIn,
    Hypothesis,
    HypothesisStatus,
    Message,
    MetricSuggestion,
    PanelRef,
    Scope,
    StatisticRef,
    Thread,
    TimeSpan,
    Verdict,
)
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.store import Panel, WorkspaceStore

BRIEF_BUDGET_BYTES = 4096
DEFAULT_HIGHLIGHT_TTL_MS = 300_000
MAX_CLAUDE_BATCH = 200
MAX_SEARCH = 200
CLAUDE_MAX_CONFIDENCE = 0.9
CURATED_ORIGINS = frozenset({"pack", "claude", "user"})  # a scan never overrules these
NONNEG_BOUNDS = frozenset({"≥0", "[0,1]", "[0,100]"})


def atomic[F: Callable](fn: F) -> F:
    """Validate `actor`, then run the store writes and their event(s) in one transaction."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        actor = sig.bind(self, *args, **kwargs).arguments["actor"]
        check_actor(actor)
        with self.log.transaction():
            return fn(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _claude_gate(item: dict[str, Any], cap: float) -> tuple[float, str]:
    """Confidence and basis rules for anything Claude writes into the catalog."""
    confidence = item.get("confidence")
    if (
        not isinstance(confidence, int | float)
        or isinstance(confidence, bool)
        or not 0 < confidence <= cap
    ):
        raise ValueError(
            f"confidence must be in (0, {cap}]: 1.0 is reserved for what the user verified"
        )
    basis = item.get("basis")
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError("basis is required: one line saying what you checked")
    return float(confidence), basis.strip()


@dataclass
class WorkspaceService:
    workspace: WorkspaceStore
    objects: ObjectStore
    datasets: DatasetStore
    log: EventLog
    catalog: CatalogStore
    relations: RelationStore
    samples: SampleStore
    families: FamilyStore
    clock: Callable[[], int] = now_ms
    packs: PackIndex = field(default_factory=builtin_packs)

    # annotations --------------------------------------------------------
    @atomic
    def annotate(self, data: AnnotationIn, actor: Actor) -> Annotation:
        if data.panel is not None:
            self.workspace.get_panel(data.panel)
        a = self.objects.create_annotation(data, actor)
        self.log.append(
            actor,
            "annotation.created",
            a.id,
            {"kind": a.kind, "panel": a.panel, "label": a.label},
        )
        return a

    @atomic
    def delete_annotation(self, annotation_id: str, actor: Actor) -> Annotation:
        a = self.objects.delete_annotation(annotation_id)
        self.log.append(actor, "annotation.deleted", a.id, {})
        return a

    # hypotheses ---------------------------------------------------------
    @atomic
    def hypothesis_create(self, statement: str, actor: Actor) -> Hypothesis:
        h = self.objects.create_hypothesis(statement, actor)
        self.log.append(actor, "hypothesis.created", h.id, {"statement": h.statement})
        return h

    @atomic
    def hypothesis_update(
        self, hypothesis_id: str, status: HypothesisStatus, actor: Actor, note: str | None = None
    ) -> Hypothesis:
        old, h = self.objects.set_hypothesis_status(hypothesis_id, status)
        self.log.append(
            actor, "hypothesis.status_changed", h.id, {"from": old, "to": h.status, "note": note}
        )
        return h

    # findings -----------------------------------------------------------
    def _evidence_datasets(self, data: FindingIn) -> list[str]:
        out: list[str] = []
        for ref in data.evidence:
            match ref:
                case PanelRef(panel=pid):
                    out.extend(self.workspace.get_panel(pid).dataset_ids[:1])
                case StatisticRef(dataset=did):
                    out.append(did)
        return list(dict.fromkeys(out))

    def _check_claim_coverage(self, data: FindingIn) -> FindingIn:
        """Reject a claim whose window lacks the data to support it; warn on partial coverage."""
        span = data.scope.time_range
        blocking: list[str] = []
        warnings: list[str] = []
        for did in self._evidence_datasets(data):
            if self.datasets.meta(did).representation not in ("bucket_agg", "quantile"):
                continue
            meta, result = self.datasets.get(did)
            states = dataset_bundle(self.datasets, meta, result).companions.get("bucket_state")
            if states is None:
                continue
            for c in claim_coverage(states, span.start_ms, span.end_ms, meta.step_ms):
                (blocking if c.severity == "blocks_claim" else warnings).append(
                    f"{did}: {c.message}"
                )
        if blocking:
            raise ValueError(
                "insufficient coverage for this claim: "
                + " ".join(blocking)
                + " (hint: narrow scope.time_range or the selector to data that exists)"
            )
        if warnings:
            data = data.model_copy(update={"caveats": [*data.caveats, *warnings]})
        return data

    @atomic
    def finding_create(self, data: FindingIn, actor: Actor) -> Finding:
        for ref in data.evidence:
            match ref:
                case PanelRef(panel=pid):
                    self.workspace.get_panel(pid)
                case StatisticRef(dataset=did):
                    if not self.datasets.exists(did):
                        raise NotFound(f"dataset {did} not found")
                case AnnotationRef(annotation=aid):
                    self.objects.get_annotation(aid)
        data = self._check_claim_coverage(data)
        if data.hypothesis is not None:
            self.objects.get_hypothesis(data.hypothesis)
        if data.answers_panel is not None:
            self.workspace.get_panel(data.answers_panel)
        f = self.objects.create_finding(data, actor)
        if data.hypothesis is not None and data.stance is not None:
            self.objects.link_evidence(data.hypothesis, f.id, data.stance)
        self.log.append(
            actor,
            "finding.created",
            f.id,
            {
                "claim": f.claim,
                "hypothesis": f.hypothesis,
                "stance": f.stance,
                "answers_panel": f.answers_panel,
            },
        )
        if data.answers_panel is not None:
            self.workspace.set_answered(data.answers_panel, f.id)
            self.log.append(actor, "panel.answered", data.answers_panel, {"finding": f.id})
        return f

    @atomic
    def finding_verdict(
        self, finding_id: str, verdict: Verdict, actor: Actor, comment: str | None = None
    ) -> Finding:
        f = self.objects.set_verdict(finding_id, verdict, comment)
        self.log.append(
            actor,
            "finding.verdict",
            f.id,
            {"verdict": verdict, "comment": comment, "claim": f.claim},
        )
        return f

    # gaps ---------------------------------------------------------------
    @atomic
    def gap_create(self, data: GapIn, actor: Actor) -> Gap:
        g = self.objects.create_gap(data, actor)
        self.log.append(
            actor,
            "gap.created",
            g.id,
            {"missing_signal": g.missing_signal, "needed_for": g.needed_for},
        )
        return g

    # threads ------------------------------------------------------------
    @atomic
    def ask(
        self,
        text: str,
        actor: Actor,
        anchor: str | None = None,
        selection: TimeSpan | None = None,
    ) -> Thread:
        if not text.strip():
            raise ValueError("message text must not be empty")
        if anchor is not None:
            self._check_anchor(anchor)
        t = self.objects.create_thread(anchor, selection, actor)
        m = self.objects.add_message(t.id, text, actor)
        self._message_event(t.id, m, actor, anchor, selection)
        return self.objects.get_thread(t.id)

    @atomic
    def post_message(self, thread_id: str, text: str, actor: Actor) -> Message:
        if not text.strip():
            raise ValueError("message text must not be empty")
        t = self.objects.get_thread(thread_id)
        m = self.objects.add_message(thread_id, text, actor)
        self._message_event(thread_id, m, actor, t.anchor, t.selection)
        return m

    def _get_gap(self, gap_id: str) -> Gap:
        for g in self.objects.list_gaps():
            if g.id == gap_id:
                return g
        raise NotFound(f"gap {gap_id} not found")

    def _check_anchor(self, anchor: str) -> None:
        checks = {
            "p": self.workspace.get_panel,
            "a": self.objects.get_annotation,
            "h": self.objects.get_hypothesis,
            "f": self.objects.get_finding,
            "g": self._get_gap,
        }
        check = checks.get(anchor[:1])
        if check is None or not anchor[1:].isdigit():
            raise ValueError(f"invalid anchor {anchor!r}; expected a p*/a*/h*/f*/g* object id")
        check(anchor)

    def _message_event(
        self, thread_id: str, m: Message, actor: Actor, anchor: str | None, sel: TimeSpan | None
    ) -> None:
        self.log.append(
            actor,
            "thread.message",
            thread_id,
            {
                "thread": thread_id,
                "message": m.id,
                "text": m.text,
                "anchor": anchor,
                "selection": sel.model_dump() if sel else None,
            },
        )

    # panels / focus -----------------------------------------------------
    @atomic
    def close_panel(self, panel_id: str, actor: Actor) -> Panel:
        p = self.workspace.close_panel(panel_id)
        self.log.append(actor, "panel.closed", p.id, {})
        return p

    @atomic
    def set_focus(self, span: TimeSpan, actor: Actor) -> None:
        self.log.append(
            actor, "focus.changed", None, {"start_ms": span.start_ms, "end_ms": span.end_ms}
        )

    # catalog ------------------------------------------------------------
    @staticmethod
    def _claim_confidence(origin: str, actor: str, confidence: float | None) -> float:
        if origin not in ORIGIN_RANK:
            raise ValueError(f"unknown origin {origin!r}; expected one of {sorted(ORIGIN_RANK)}")
        if (origin == "user") != (actor == "user"):
            raise ValueError("origin 'user' is reserved for the user's own edits (and vice versa)")
        if confidence is None:
            if origin != "user":
                raise ValueError("confidence is required for non-user claims")
            return 1.0
        return confidence

    @atomic
    def catalog_claim(
        self,
        source: str,
        metric: str,
        field: FieldName,
        value: Any,
        origin: Origin,
        actor: Actor,
        *,
        confidence: float | None = None,
        verified_by: str | None = None,
        citation: str | None = None,
    ) -> Claim:
        """Record `origin`'s claim on a metric field; the resolved value is computed on read."""
        confidence = self._claim_confidence(origin, actor, confidence)
        validate_value(field, value)
        claim = Claim(
            field=field,
            value=value,
            origin=origin,
            confidence=confidence,
            verified_by=verified_by,
            citation=citation,
            ts_ms=self.clock(),
        )
        self.catalog.put_claim(source, metric, claim)
        self.log.append(
            actor,
            "catalog.claimed",
            None,
            {
                "source": source,
                "metric": metric,
                "field": field,
                "value": value,
                "origin": origin,
                "confidence": confidence,
            },
        )
        if field in ("unit", "type"):
            self._refresh_panel_units(source, metric)
        return claim

    @atomic
    def record_scan(
        self,
        source: str,
        metric: str,
        stats: SampleStats,
        dataset: str,
        window_ms: int,
        step_ms: int,
        actor: Actor = "system",
    ) -> dict[str, Any]:
        """Store a sample scan; write the `stats` claims it justifies and file contradictions.

        A stats claim never replaces a pack, Claude or user claim that disagrees (the
        disagreement becomes a finding instead); it may replace a name rule or declared metadata.
        Bounds are only claimed to fill a gap, so they cannot degrade a known [0,1]."""
        now = self.clock()
        self.samples.put(
            source,
            metric,
            SampleObservation(
                window_ms=window_ms, step_ms=step_ms, series=stats.series, voting=stats.voting,
                n=stats.n, min=stats.min, max=stats.max, negatives=stats.negatives,
                increases=stats.increases, decreases=stats.decreases, resets=stats.resets,
                small_decreases=stats.small_decreases, gauge_voters=stats.gauge_voters,
                integral=stats.integral, constant=stats.constant, verdict=stats.verdict,
                dataset=dataset, scanned_ms=now,
            ),
        )  # fmt: skip
        others = [c for c in self.catalog.claims_for(source, metric) if c.origin != "stats"]
        declared_type = resolve(c for c in others if c.field == "type")
        declared_bounds = resolve(c for c in others if c.field == "bounds")
        window = format_duration(window_ms)
        claims: list[str] = []

        evidence_type = "counter" if stats.counter_like else "gauge" if stats.gauge_like else None
        if evidence_type:
            curated_disagrees = (
                declared_type is not None
                and declared_type.origin in CURATED_ORIGINS
                and declared_type.value != evidence_type
            )
            if not curated_disagrees:
                self._put_stats_claim(
                    source, metric, "type", evidence_type, 0.6, f"sample scan over {window}", now
                )
                claims.append("type")
        if stats.nonnegative and declared_bounds is None:
            self._put_stats_claim(
                source, metric, "bounds", "≥0", 0.4, f"no negative samples in {window}", now
            )
            claims.append("bounds")

        findings = []
        basis = (
            "(a name convention only)"
            if declared_type is not None and declared_type.origin == "rule"
            else f"(declared by {declared_type.origin})"
            if declared_type is not None
            else ""
        )
        checks = [
            (
                "gauge_grows",
                declared_type is not None and declared_type.value == "gauge" and stats.grows_only,
                f"{metric} is declared a gauge {basis} but only increased over {window}",
                "increases", stats.increases,
            ),
            (
                "counter_decreases",
                declared_type is not None and declared_type.value == "counter" and stats.small_decreases > 0,
                f"{metric} is declared a counter {basis} but decreased without resetting over {window}",
                "small_decreases", stats.small_decreases,
            ),
            (
                "negative_values",
                stats.negatives > 0
                and (
                    (declared_type is not None and declared_type.value == "counter")
                    or (declared_bounds is not None and declared_bounds.value in NONNEG_BOUNDS)
                ),
                f"{metric} has negative samples although it is claimed non-negative over {window}",
                "negatives", stats.negatives,
            ),
        ]  # fmt: skip
        for kind, hit, claim, name, count in checks:
            if not hit or self.samples.finding(source, metric, kind):
                continue
            f = self.finding_create(
                FindingIn(
                    claim=claim,
                    scope=Scope(
                        source=source,
                        selector=metric,
                        time_range=TimeSpan(start_ms=now - window_ms, end_ms=now),
                        step=format_duration(step_ms),
                        aggregation="raw samples (one per step)",
                    ),
                    evidence=[
                        StatisticRef(
                            kind="statistic", dataset=dataset, name=name, value=float(count),
                            exact=True, method="sample scan",
                            params={"series": stats.series, "samples": stats.n, "window": window},
                        )
                    ],
                    caveats=["short_window"],
                ),
                actor,
            )  # fmt: skip
            self.samples.set_finding(source, metric, kind, f.id)
            findings.append(f.id)
        self.log.append(
            actor, "catalog.scanned", None,
            {"source": source, "metric": metric, "verdict": stats.verdict, "claims": claims, "findings": findings},
        )  # fmt: skip
        return {"claims": claims, "findings": findings}

    def _put_stats_claim(
        self,
        source: str,
        metric: str,
        field: str,
        value: Any,
        confidence: float,
        basis: str,
        now: int,
    ) -> None:
        validate_value(field, value)
        self.catalog.put_claim(
            source,
            metric,
            Claim(
                field=field, value=value, origin="stats", confidence=confidence,
                citation=basis, ts_ms=now,
            ),
        )  # fmt: skip
        if field in ("unit", "type"):
            self._refresh_panel_units(source, metric)

    def _refresh_panel_units(self, source: str, metric: str) -> None:
        """Panels draw their axis unit from the catalog: when a claim changes a metric's unit (or
        its type, which decides rate units), re-derive it for open panels that use the metric.
        A unit someone stated explicitly when the panel was shown ("provided by ...") stays."""
        for p in self.workspace.list_panels():
            if p.closed or not p.dataset_ids:
                continue
            try:
                meta = self.datasets.meta(p.dataset_ids[0])
                spec = ChartSpec.model_validate(p.spec)
            except (NotFound, ValueError):
                continue
            if meta.source != source or metric not in metric_names(meta.expr):
                continue
            if (spec.y.unit_provenance or "").startswith("provided by"):
                continue
            unit, why = infer_unit_with_provenance(
                meta.expr, lambda m: self.catalog_facts(source, m)
            )
            if (unit, why) == (spec.y.unit, spec.y.unit_provenance):
                continue
            spec.y.unit, spec.y.unit_provenance = unit, why
            self.workspace.set_spec(p.id, spec.model_dump())
            self.log.append(
                "system",
                "panel.unit_refreshed",
                p.id,
                {"metric": metric, "unit": unit, "provenance": why},
            )

    @atomic
    def catalog_relearn(
        self, source: str, names: Collection[str], actor: Actor, *, complete: bool = True
    ) -> RelearnDiff:
        """Reconcile the inventory with a fresh metric listing; claims survive removal."""
        diff = self.catalog.relearn(source, names, self.clock(), complete=complete)
        self.log.append(
            actor,
            "catalog.relearned",
            None,
            {
                "source": source,
                "complete": complete,
                "new": len(diff.new),
                "removed": len(diff.removed),
                "returned": len(diff.returned),
            },
        )
        return diff

    @atomic
    def catalog_learn(
        self, source: str, discovery: Discovery, actor: Actor = "system"
    ) -> dict[str, Any]:
        """Re-learn a source from its discovery: reconcile the inventory, then write T0 claims
        (declared metadata + name rules) in bulk. One event, not one per claim."""
        names = [m.name for m in discovery.metrics]
        complete = not any(c.startswith("metrics_truncated") for c in discovery.caveats)
        diff = self.catalog.relearn(source, names, self.clock(), complete=complete)
        ts = self.clock()
        # names that encode a dimension (airflow_ti_finish_<dag>_<task>) collapse into families
        detection = detect_families(names)
        rejected = self.families.rejected(source)
        assignment = {n: ta for n, ta in detection.assignment.items() if ta[0] not in rejected}
        fam = self.families.apply(source, detection, ts)
        rows = [
            (m.name, spec.to_claim(ts))
            for m in discovery.metrics
            if m.name not in assignment  # a family speaks for its members
            for spec in [
                *derive_claims(m.name, m, discovery.histograms),
                *self.packs.claims_for(m.name),
            ]
        ]
        rows += family_claims(discovery.metrics, assignment, detection, rejected, ts)
        changed = self.catalog.put_claims_bulk(source, rows)
        name_set = set(names)
        pack_relations = [
            RelationClaim(
                source=source,
                subject=m.name,
                kind=spec.kind,  # type: ignore[arg-type]
                object=spec.object,
                origin="pack",
                confidence=spec.confidence,
                basis=spec.basis,
                ts_ms=ts,
            )
            for m in discovery.metrics
            for spec in self.packs.relations_for(m.name)
            if spec.object in name_set and spec.object != m.name
        ]
        relations_changed = self.relations.put_relations(pack_relations)
        summary = {
            "source": source,
            "metrics": len(names),
            "new": len(diff.new),
            "removed": len(diff.removed),
            "returned": len(diff.returned),
            "claims_changed": changed,
            "relations_changed": relations_changed,
            "families": fam["families"],
            "family_members": fam["members"],
            "complete": complete,
            "caveats": list(discovery.caveats),
        }
        self.log.append(actor, "catalog.learned", None, summary)
        return summary

    # relations and bindings --------------------------------------------
    def _check_endpoint(self, level: str, source: str, name: str) -> None:
        if level == "catalog":
            if not self.catalog.has_metric(source, name):
                raise NotFound(f"unknown metric {name!r} on {source!r}; run source_learn first")
        elif level == "workspace":
            if not self.datasets.exists(name):
                raise NotFound(f"dataset {name!r} not found")
        else:
            raise ValueError(f"unknown level {level!r}; expected 'catalog' or 'workspace'")

    @atomic
    def relate(
        self,
        source: str,
        subject: str,
        kind: str,
        target: str,
        origin: Origin,
        actor: Actor,
        *,
        confidence: float | None = None,
        basis: str | None = None,
        params: dict[str, Any] | None = None,
        retract: bool = False,
        level: Level = "catalog",
    ) -> ResolvedRelation:
        """Claim (or retract) a typed edge between two metrics (catalog) or datasets (workspace)."""
        confidence = self._claim_confidence(origin, actor, confidence)
        params = dict(params or {})
        validate_relation(kind, subject, target, params)
        subject, target = canonical_ends(kind, subject, target)
        for end in (subject, target):
            self._check_endpoint(level, source, end)
        scope = source if level == "catalog" else ""
        self.relations.put_relations(
            [
                RelationClaim(
                    level=level,
                    source=scope,
                    subject=subject,
                    kind=kind,  # type: ignore[arg-type]
                    object=target,
                    origin=origin,
                    confidence=confidence,
                    retracted=retract,
                    params=params,
                    basis=basis,
                    ts_ms=self.clock(),
                )
            ]
        )
        self.log.append(
            actor,
            "relation.claimed",
            None,
            {
                "level": level, "source": scope, "subject": subject, "kind": kind,
                "object": target, "origin": origin, "retracted": retract, "confidence": confidence,
            },
        )  # fmt: skip
        return next(
            r
            for r in self.relations.relations(level, scope, kind=kind, include_retracted=True)
            if (r.subject, r.object) == (subject, target)
        )

    @atomic
    def bind(
        self,
        source: str,
        kind: str,
        key: str,
        roles: dict[str, str | None],
        origin: Origin,
        actor: Actor,
        *,
        join_on: list[str] | None = None,
        confidence: float | None = None,
        basis: str | None = None,
        retract: bool = False,
        level: Level = "catalog",
    ) -> dict[str, Any]:
        """Claim (or retract) a role-based model binding. An unfilled role in the winning
        claim raises a Gap recommending the missing instrumentation (once per role)."""
        confidence = self._claim_confidence(origin, actor, confidence)
        join = list(join_on or [])
        validate_binding(kind, roles, join)
        if not key.strip():
            raise ValueError("binding key (service/resource name) is required")
        for metric in roles.values():
            if metric is not None:
                self._check_endpoint(level, source, metric)
        scope = source if level == "catalog" else ""
        self.relations.put_binding(
            BindingClaim(
                level=level,
                source=scope,
                kind=kind,  # type: ignore[arg-type]
                key=key,
                origin=origin,
                confidence=confidence,
                retracted=retract,
                roles=dict(roles),
                join_on=join,
                basis=basis,
                ts_ms=self.clock(),
            )
        )
        self.log.append(
            actor,
            "binding.claimed",
            None,
            {
                "level": level, "source": scope, "kind": kind, "key": key, "origin": origin,
                "retracted": retract, "roles": dict(roles), "confidence": confidence,
            },
        )  # fmt: skip
        resolved = next(
            b
            for b in self.relations.bindings(
                level, scope, kind=kind, key=key, include_retracted=True
            )
        )
        gaps = (
            []
            if resolved.winner.retracted
            else self._gaps_for_unfilled(level, scope, resolved, actor)
        )
        return {"binding": resolved, "gaps": gaps}

    def _gaps_for_unfilled(
        self, level: str, scope: str, resolved: ResolvedBinding, actor: Actor
    ) -> list[str]:
        created = []
        slug = metric_slug(resolved.key)
        for role, metric in resolved.winner.roles.items():
            if metric is not None or self.relations.binding_gap(
                level, scope, resolved.kind, resolved.key, role
            ):
                continue
            hint = SUGGESTIONS[(resolved.kind, role)]
            gap = self.gap_create(
                GapIn(
                    missing_signal=f"{role} signal for {resolved.kind} on '{resolved.key}'",
                    needed_for=f"{resolved.kind} model of '{resolved.key}': {hint.why}",
                    suggestion=MetricSuggestion(
                        name=hint.name.format(key=slug), type=hint.type, labels=list(hint.labels)
                    ),
                ),
                actor,
            )
            self.relations.set_binding_gap(level, scope, resolved.kind, resolved.key, role, gap.id)
            created.append(gap.id)
        return created

    def catalog_relations(
        self,
        source: str,
        metric: str | None = None,
        kind: str | None = None,
        level: Level = "catalog",
        include_retracted: bool = False,
    ) -> dict[str, list]:
        if level not in ("catalog", "workspace"):
            raise ValueError(f"unknown level {level!r}; expected 'catalog' or 'workspace'")
        scope = source if level == "catalog" else ""
        rels = self.relations.relations(
            level, scope, metric=metric, kind=kind, include_retracted=include_retracted
        )
        binds = [
            b
            for b in self.relations.bindings(level, scope, include_retracted=include_retracted)
            if metric is None or metric in b.winner.roles.values()
        ]
        return {"relations": rels, "bindings": binds}

    def relate_claude(
        self, source: str, items: list[dict[str, Any]], level: Level = "catalog"
    ) -> list[dict]:
        """Claude's batched relation writes: origin claude, basis required, confidence capped
        (correlated is evidence, not truth: lower cap). Per-item rejection; outranked claims are
        stored but reported."""
        if len(items) > MAX_CLAUDE_BATCH:
            raise ValueError(f"at most {MAX_CLAUDE_BATCH} claims per call, got {len(items)}")
        results = []
        for item in items:
            base = {k: item.get(k) for k in ("subject", "kind", "object")}
            try:
                cap = (
                    CORRELATED_MAX_CONFIDENCE
                    if item.get("kind") == "correlated"
                    else CLAUDE_MAX_CONFIDENCE
                )
                confidence, basis = _claude_gate(item, cap)
                r = self.relate(
                    source,
                    str(item.get("subject")),
                    str(item.get("kind")),
                    str(item.get("object")),
                    "claude",
                    "claude",
                    confidence=confidence,
                    basis=basis,
                    params=item.get("params"),
                    retract=bool(item.get("retract", False)),
                    level=level,
                )
            except (NotFound, ValueError) as e:
                results.append({**base, "status": "rejected", "reason": str(e)})
                continue
            effective = r.winner.origin == "claude"
            res = {**base, "status": "accepted", "effective": effective}
            if not effective:
                res["outranked_by"] = r.winner.origin
            results.append(res)
        return results

    def bind_claude(
        self,
        source: str,
        kind: str,
        key: str,
        roles: dict[str, str | None],
        *,
        join_on: list[str] | None = None,
        confidence: float | None = None,
        basis: str | None = None,
        retract: bool = False,
        level: Level = "catalog",
    ) -> dict[str, Any]:
        conf, why = _claude_gate({"confidence": confidence, "basis": basis}, CLAUDE_MAX_CONFIDENCE)
        out = self.bind(
            source, kind, key, roles, "claude", "claude",
            join_on=join_on, confidence=conf, basis=why, retract=retract, level=level,
        )  # fmt: skip
        win = out["binding"].winner
        effective = win.origin == "claude"
        return {
            "binding": {
                "kind": kind,
                "key": key,
                "roles": win.roles,
                "join_on": win.join_on,
                "retracted": win.retracted,
            },
            "effective": effective,
            **({} if effective else {"outranked_by": win.origin}),
            "gaps": out["gaps"],
        }

    def catalog_hot(self, source: str) -> set[str]:
        """Catalogued metrics this workspace has actually queried (any dataset expression)."""
        known = {e.metric for e in self.catalog.list_entries(source)}
        used: set[str] = set()
        for meta in self.datasets.list_metas():
            if meta.source == source:
                used |= metric_names(meta.expr)
        return used & known

    def catalog_search(
        self,
        source: str,
        query: str | None = None,
        prefix: str | None = None,
        needs_review: bool = False,
        limit: int = 50,
    ) -> dict:
        entries = self.catalog.list_entries(source)
        return search_entries(
            entries,
            self.catalog_hot(source),
            query=query,
            prefix=prefix,
            needs_review=needs_review,
            limit=max(1, min(limit, MAX_SEARCH)),
        )

    def catalog_browse(self, source: str, b: Browse) -> dict:
        """One page of the catalog with provenance per row, plus counts for the whole source."""
        total, names, summary = browse_catalog(self.catalog.connection, source, b)
        findings = self.samples.findings_for(source, names)
        verdicts = self.samples.verdicts_for(source, names)
        rows = []
        for n in names:
            row = browse_row(self.catalog.entry(source, n), findings[n], verdicts.get(n))
            if row["is_family"]:
                row["family_info"] = self.families.info(source, n)
            rows.append(row)
        return {
            "source": source,
            "total": total,
            "offset": b.offset,
            "rows": rows,
            "summary": summary,
        }

    @atomic
    def catalog_family_decide(
        self,
        source: str,
        template: str,
        action: str,
        origin: Origin,
        actor: Actor,
        *,
        basis: str | None = None,
    ) -> dict[str, Any]:
        """Confirm or split a name-template family. Confirming pins it against re-detection;
        splitting dissolves it for good. Anyone may decide, but nobody overrides the user."""
        if action not in ("confirm", "split"):
            raise ValueError("action must be 'confirm' or 'split'")
        if (origin == "user") != (actor == "user"):
            raise ValueError(
                "origin 'user' is reserved for the user's own decisions (and vice versa)"
            )
        info = self.families.info(source, template)
        if info is None:
            raise NotFound(f"no family {template!r} on {source!r}")
        if info["status"] == "confirmed" and info["decided_by"] == "user" and actor != "user":
            raise ValueError("the user confirmed this family; only the user can change that")
        now = self.clock()
        if action == "confirm":
            self.families.confirm(source, template, origin, basis, now)
            released = 0
        else:
            released = self.families.split(source, template, origin, now)
        self.log.append(
            actor,
            "catalog.family_confirmed" if action == "confirm" else "catalog.family_split",
            None,
            {
                "source": source,
                "template": template,
                "origin": origin,
                "members": info["members"],
                "released": released,
            },
        )
        return {
            "template": template,
            "action": action,
            "members": info["members"],
            "released": released,
        }

    def family_members(self, source: str, template: str, offset: int = 0, limit: int = 50) -> dict:
        info = self.families.info(source, template)
        if info is None:
            raise NotFound(f"no family {template!r} on {source!r}")
        rows = self.families.members(source, template, offset, min(limit, 200))
        return {
            **info,
            "offset": offset,
            "members_page": [{"metric": m, "dimension": d} for m, d in rows],
        }

    def metric_section(self, source: str, metric: str) -> dict:
        """What the card and the catalog view show for one metric: fields with every competing
        claim, relations, bindings, and the gaps those bindings raised."""
        entry = self.catalog.entry(source, metric)
        rels = self.catalog_relations(source, metric)
        gaps = []
        for b in rels["bindings"]:
            for role, gid in self.relations.binding_gaps("catalog", source, b.kind, b.key).items():
                gaps.append({"id": gid, "binding": f"{b.kind}/{b.key}", "role": role})
        family = None
        if entry.is_family:
            family = {"role": "family", **(self.families.info(source, metric) or {})}
        elif entry.family:
            family = {"role": "member", "template": entry.family, "dimension": entry.dimension,
                      "inherited": entry.inherited_from is not None}  # fmt: skip
        return {
            "metric": metric,
            "present": entry.present,
            "family": family,
            "fields": field_rows(entry),
            "relations": [relation_row(r) for r in rels["relations"]],
            "bindings": [binding_row(b) for b in rels["bindings"]],
            "gaps": gaps,
        }

    def catalog_overview(self, source: str, top: int = 30) -> list[dict]:
        return family_overview(self.catalog.list_entries(source), top)

    def catalog_write_claude(self, source: str, items: list[dict[str, Any]]) -> list[dict]:
        """Claude's batched catalog writes. Origin is always `claude`; every claim needs a
        `basis` and confidence is capped; bad items are rejected one by one, never the batch.
        A claim that a higher-ranked origin (user, ...) outranks is stored but reported as such."""
        if len(items) > MAX_CLAUDE_BATCH:
            raise ValueError(f"at most {MAX_CLAUDE_BATCH} claims per call, got {len(items)}")
        results = []
        for item in items:
            metric, fld = str(item.get("metric")), str(item.get("field"))
            base = {"metric": metric, "field": fld}
            try:
                confidence = item.get("confidence")
                if (
                    not isinstance(confidence, int | float)
                    or not 0 < confidence <= CLAUDE_MAX_CONFIDENCE
                ):
                    raise ValueError(
                        f"confidence must be in (0, {CLAUDE_MAX_CONFIDENCE}]: 1.0 is reserved "
                        "for what the user verified"
                    )
                basis = item.get("basis")
                if not isinstance(basis, str) or not basis.strip():
                    raise ValueError("basis is required: one line saying what you checked")
                self.catalog.entry(source, metric)  # NotFound if never learned
                self.catalog_claim(
                    source,
                    metric,
                    fld,  # type: ignore[arg-type]
                    item.get("value"),
                    "claude",
                    "claude",
                    confidence=float(confidence),
                    citation=basis.strip(),
                )
            except NotFound:
                results.append(
                    {
                        **base,
                        "status": "rejected",
                        "reason": f"unknown metric {metric!r} on {source!r}; run source_learn first",
                    }
                )
                continue
            except ValueError as e:
                results.append({**base, "status": "rejected", "reason": str(e)})
                continue
            win = self.catalog.entry(source, metric).fields[fld]
            effective = win.origin == "claude" and win.value == item.get("value")
            res = {**base, "status": "accepted", "effective": effective}
            if not effective:
                res["outranked_by"] = win.origin
            results.append(res)
        return results

    def catalog_bounds(self, source: str, metric: str) -> tuple[str, str] | None:
        """(bounds claim, origin) for a metric: the catalog winner, else the name rules alone."""
        claims = self.catalog.claims_for(source, metric)
        if not claims:
            claims = [c.to_claim(0) for c in derive_claims(metric)]
        win = resolve(c for c in claims if c.field == "bounds")
        return (win.value, win.origin) if win else None

    def catalog_bounded_by(self, source: str, metric: str) -> list[str]:
        """Metrics `metric` never exceeds (at the same labels), strongest claim first."""
        rels = self.relations.relations("catalog", source, metric=metric, kind="bounded_by")
        rels = [r for r in rels if r.subject == metric]
        rels.sort(key=lambda r: (-r.winner.confidence, r.object))
        return [r.object for r in rels]

    @atomic
    def set_y_context(self, panel_id: str, ctx: YContext, actor: Actor) -> Panel:
        """Record the catalog inputs to a time panel's y range (bead 2as.10)."""
        p, spec = self._time_spec(panel_id)
        spec.y.context = ctx
        spec.y.range_mode = "reference" if ctx.has_reference else "data"
        p = self.workspace.set_spec(p.id, spec.model_dump())
        self.log.append(
            actor,
            "panel.y_context",
            p.id,
            {
                "bounds": ctx.bounds,
                "limit": ctx.limit.metric if ctx.limit else None,
                "profile": ctx.profile is not None,
                "notes": ctx.notes,
            },
        )
        return p

    def catalog_facts(self, source: str, metric: str) -> Facts:
        """Unit/type for charts: the catalog's winners, else the name rules alone."""
        claims = self.catalog.claims_for(source, metric)
        return facts_from_claims(claims) if claims else facts_from_name(metric)

    def catalog_entry(self, source: str, metric: str) -> CatalogEntry:
        return self.catalog.entry(source, metric)

    def catalog_list(self, source: str, *, present_only: bool = True) -> list[CatalogEntry]:
        return self.catalog.list_entries(source, present_only=present_only)

    # highlights (UX only: events, no stored state) -----------------------
    @atomic
    def highlight(
        self,
        object_id: str,
        actor: Actor,
        note: str | None = None,
        ttl_ms: int | None = DEFAULT_HIGHLIGHT_TTL_MS,
    ) -> Event:
        """Draw attention to an object; `ttl_ms=None` keeps it until cleared."""
        self._check_anchor(object_id)
        if ttl_ms is not None and ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive (or None for until cleared)")
        return self.log.append(
            actor, "object.highlighted", object_id, {"note": note or None, "ttl_ms": ttl_ms}
        )

    @atomic
    def unhighlight(self, object_id: str, actor: Actor) -> Event:
        self._check_anchor(object_id)
        return self.log.append(actor, "object.unhighlighted", object_id, {})

    def list_panels(self) -> list[Panel]:
        return self.workspace.list_panels()

    # reads --------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "panels": [p.to_dict() for p in self.workspace.list_panels()],
            "annotations": [a.model_dump() for a in self.objects.list_annotations()],
            "hypotheses": [h.model_dump() for h in self.objects.list_hypotheses()],
            "findings": [f.model_dump() for f in self.objects.list_findings()],
            "gaps": [g.model_dump() for g in self.objects.list_gaps()],
            "threads": self._threads_with_seqs(),
            "last_seq": self.log.last_seq,
        }

    def _threads_with_seqs(self) -> list[dict]:
        seqs = self.log.message_seqs()
        threads = [t.model_dump() for t in self.objects.list_threads()]
        for t in threads:
            for m in t["messages"]:
                m["seq"] = seqs.get(m["id"])
        return threads

    # y-views (spec §6.2, bead 2as.17) ----------------------------------
    def _time_spec(self, panel_id: str) -> tuple[Panel, ChartSpec]:
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        if any(layer.mark != "line+envelope" for layer in spec.layers):
            raise ValueError(
                f"y-views apply to time-series panels; {p.id} is a heatmap/histogram "
                "with its own value-axis controls"
            )
        return p, spec

    def _check(self, p: Panel, view: YView, spec: ChartSpec | None = None) -> list[str]:
        meta, result = self.datasets.get(p.dataset_ids[0])
        if view.mode == "indexed":
            assert view.baseline is not None
            ref = (
                (spec.references.get(view.baseline) if spec else None)
                if view.baseline != "window"
                else None
            )
            on_grid = None
            if ref is not None:
                on_grid = shifted(self.datasets.get(ref.series)[1].buckets, ref.shift_ms)
            labels = {r["series_id"]: r["labels"] for r in result.series.to_pylist()}
            return check_index(
                view.baseline, meta.representation, meta.n_min, result.buckets, on_grid, labels
            )
        return check_view(
            view,
            value_stats(result.buckets, meta.representation, meta.n_min),
            spec.y.context if spec else None,
        )

    @atomic
    def suggest_y_view(
        self,
        panel_id: str,
        view: YView,
        actor: Actor,
        replace: bool = False,
        reference: Reference | None = None,
    ) -> tuple[YView, list[str]]:
        p, spec = self._time_spec(panel_id)
        if reference is not None:
            spec.references[reference.mode] = reference
        warnings = self._check(p, view, spec)
        keep = [] if replace else [v for v in spec.y.views if v.label != view.label]
        if len(keep) >= MAX_SUGGESTIONS:
            raise ValueError(
                f"{p.id} already has {MAX_SUGGESTIONS} suggested views; pass replace=true to start over"
            )
        n = 1 + max((int(v.id[1:]) for v in spec.y.views if v.id), default=0)
        saved = YView.model_validate({**view.model_dump(), "id": f"v{n}", "author": actor})
        spec.y.views = [*keep, saved]
        self.workspace.set_spec(p.id, spec.model_dump())
        self.log.append(
            actor,
            "panel.y_view_suggested",
            p.id,
            {"view": saved.model_dump(exclude_none=True), "warnings": warnings},
        )
        return saved, warnings

    @atomic
    def select_y_view(
        self,
        panel_id: str,
        actor: Actor,
        *,
        mode: str | None = None,
        lo: float | None = None,
        hi: float | None = None,
        suggestion: str | None = None,
        baseline: str | None = None,
        reference: Reference | None = None,
    ) -> Panel:
        p, spec = self._time_spec(panel_id)
        if reference is not None:
            spec.references[reference.mode] = reference
        if suggestion is not None:
            view = next((v for v in spec.y.views if v.id == suggestion), None)
            if view is None:
                raise NotFound(f"view {suggestion} not found on {p.id}")
        elif mode is None:
            raise ValueError("give a mode or a suggestion id")
        else:
            label = INDEX_LABELS.get(baseline or "", "") if mode == "indexed" else None
            view = YView.model_validate(
                {
                    "mode": mode,
                    "label": label or BUILTIN_LABELS.get(mode, mode),
                    "lo": lo,
                    "hi": hi,
                    "baseline": baseline,
                }
            )
        self._check(p, view, spec)  # refusals raise; band warnings are shown in the UI as clipping
        spec.y.selected = None if view.mode == "auto" else view
        p = self.workspace.set_spec(p.id, spec.model_dump())
        self.log.append(
            actor,
            "panel.y_view_selected",
            p.id,
            {
                "mode": view.mode,
                "label": view.label,
                "lo": view.lo,
                "hi": view.hi,
                "suggestion": view.id,
                "reason": view.reason,
                "baseline": view.baseline,
            },
        )
        return p

    @atomic
    def select_data_view(self, panel_id: str, view: str, actor: Actor) -> Panel:
        """Pick filtered / raw / overlay / removed on a panel drawn from filter() (bead 4ok.9)."""
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        if spec.signal is None:
            raise ValueError("data views apply to panels drawn from filter()")
        spec.signal = SignalViews.model_validate({**spec.signal.model_dump(), "selected": view})
        p = self.workspace.set_spec(p.id, spec.model_dump())
        self.log.append(
            actor,
            "panel.data_view_selected",
            p.id,
            {"view": view, "default": spec.signal.default, "filter": spec.signal.filter},
        )
        return p

    @atomic
    def set_marginal(
        self, panel_id: str, marginal: Marginal | None, reference: Reference | None, actor: Actor
    ) -> Panel:
        p, spec = self._time_spec(panel_id)
        if reference is not None:
            spec.references[reference.mode] = reference
        spec.marginal = marginal
        p = self.workspace.set_spec(p.id, spec.model_dump())
        self.log.append(
            actor,
            "panel.marginal_set",
            p.id,
            {
                "reference": marginal.reference if marginal else None,
                "label": reference.label if (marginal and reference) else None,
                "reason": marginal.reason if marginal else None,
            },
        )
        return p

    @atomic
    def set_overlays(
        self,
        panel_id: str,
        actor: Actor,
        *,
        normal: bool | None = None,
        limit: bool | None = None,
        ghost: bool | None = None,
        reference: Reference | None = None,
    ) -> Panel:
        """Switch reference layers on or off (bead 2as.11); only the given flags change."""
        p, spec = self._time_spec(panel_id)
        for name, value in (("normal", normal), ("limit", limit), ("ghost", ghost)):
            if value is not None:
                setattr(spec.overlays, name, bool(value))
        if reference is not None:
            spec.references[reference.mode] = reference
        p = self.workspace.set_spec(p.id, spec.model_dump())
        self.log.append(actor, "panel.overlays_set", p.id, spec.overlays.model_dump())
        return p

    def brief(self) -> dict:
        """Compact state for Claude: newest first, truncated to BRIEF_BUDGET_BYTES."""
        hyps = [
            {"id": h.id, "status": h.status, "statement": h.statement}
            for h in reversed(self.objects.list_hypotheses())
        ]
        finds = [
            {"id": f.id, "claim": f.claim, "verdict": f.verdict}
            for f in reversed(self.objects.list_findings())
        ]
        open_threads = [
            {"id": t.id, "anchor": t.anchor, "last": t.messages[-1].text[:200]}
            for t in reversed(self.objects.list_threads())
            if t.messages and t.messages[-1].author == "user"
        ]
        panels = [
            {
                "id": p.id,
                "question": p.question,
                "status": p.status,
                **(
                    {"y_view": sel["label"]} if (sel := p.spec.get("y", {}).get("selected")) else {}
                ),
                **(
                    {"data_view": sv["selected"]}
                    if (sv := p.spec.get("signal")) and sv.get("selected")
                    else {}
                ),
                **(
                    {"marginal": ref["label"]}
                    if (mg := p.spec.get("marginal"))
                    and (ref := p.spec.get("references", {}).get(mg["reference"]))
                    else {}
                ),
            }
            for p in self.workspace.list_panels()
        ]
        out: dict = {
            "panels": panels,
            "hypotheses": hyps,
            "findings": finds,
            "open_threads": open_threads,
            "last_seq": self.log.last_seq,
        }
        cut = dict.fromkeys(("panels", "hypotheses", "findings", "open_threads"), 0)

        def size() -> int:
            more = {f"more_{k}": n for k, n in cut.items() if n}
            return len(json.dumps(out | more))

        while size() > BRIEF_BUDGET_BYTES:
            key = max(cut, key=lambda k: len(out[k]))
            if not out[key]:
                break
            out[key].pop()
            cut[key] += 1
        out.update({f"more_{k}": n for k, n in cut.items() if n})
        return out

    def activity(self, since: int | None = None, limit: int = 50) -> dict:
        start = self.log.last_seq - limit if since is None else since
        events = self.log.since(max(0, start), limit=limit)
        rows = [
            {
                "seq": e.seq,
                "actor": e.actor,
                "type": e.type,
                "object_id": e.object_id,
                "summary": describe_event(e),
            }
            for e in events
        ]
        last = self.log.last_seq
        next_since = rows[-1]["seq"] if rows else start
        return {
            "events": rows,
            "last_seq": last,
            "next_since": next_since,
            "truncated": next_since < last,
        }


def family_claims(infos, assignment, detection, rejected, ts: int) -> list[tuple[str, Claim]]:
    """T0 claims for family pseudo-metrics: what the cluster is, and the type/unit its members
    agree on (declared by at least 95% of them)."""
    by_family: dict[str, list] = {}
    for m in infos:
        if m.name in assignment:
            by_family.setdefault(assignment[m.name][0], []).append(m)
    out: list[tuple[str, Claim]] = []
    for f in detection.families:
        if f.template in rejected or f.template not in by_family:
            continue
        members = by_family[f.template]
        example = assignment[members[0].name][1]
        out.append(
            (
                f.template,
                Claim(
                    field="description",
                    value=(
                        f"{f.members} metrics share this name template; the part where * stands is a "
                        f"dimension encoded in the metric name (for example {example!r})."
                    ),
                    origin="rule",
                    confidence=0.5,
                    citation="name-template clustering",
                    ts_ms=ts,
                ),
            )
        )
        for field_, values in (
            ("type", [m.type for m in members]),
            ("unit", [normalize_unit(m.unit) for m in members]),
        ):
            top, n = Counter(values).most_common(1)[0]
            if top is not None and n >= 0.95 * len(members):
                out.append(
                    (
                        f.template,
                        Claim(
                            field=field_, value=top, origin="metadata", confidence=0.9,
                            citation=f"declared by {n} of {len(members)} members", ts_ms=ts,
                        ),
                    )
                )  # fmt: skip
    return out
