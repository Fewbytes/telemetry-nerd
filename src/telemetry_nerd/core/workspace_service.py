"""Reasoning operations. Every mutation appends exactly one event (documented exceptions)."""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from telemetry_nerd.catalog.models import (
    ORIGIN_RANK,
    CatalogEntry,
    Claim,
    FieldName,
    Origin,
    RelearnDiff,
    validate_value,
)
from telemetry_nerd.catalog.packs import PackIndex, builtin_packs
from telemetry_nerd.catalog.rules import Facts, derive_claims, facts_from_claims, facts_from_name
from telemetry_nerd.catalog.store import CatalogStore
from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.charts.indexed import check_index, shifted
from telemetry_nerd.charts.spec import ChartSpec, Marginal, Reference
from telemetry_nerd.charts.yview import (
    BUILTIN_LABELS,
    INDEX_LABELS,
    MAX_SUGGESTIONS,
    YView,
    check_view,
    value_stats,
)
from telemetry_nerd.core.events import Actor, Event, EventLog, check_actor
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import now_ms
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
    PanelRef,
    StatisticRef,
    Thread,
    TimeSpan,
    Verdict,
)
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.store import Panel, WorkspaceStore

BRIEF_BUDGET_BYTES = 4096
DEFAULT_HIGHLIGHT_TTL_MS = 300_000


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


@dataclass
class WorkspaceService:
    workspace: WorkspaceStore
    objects: ObjectStore
    datasets: DatasetStore
    log: EventLog
    catalog: CatalogStore
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
        if origin not in ORIGIN_RANK:
            raise ValueError(f"unknown origin {origin!r}; expected one of {sorted(ORIGIN_RANK)}")
        if (origin == "user") != (actor == "user"):
            raise ValueError("origin 'user' is reserved for the user's own edits (and vice versa)")
        if confidence is None:
            if origin != "user":
                raise ValueError("confidence is required for non-user claims")
            confidence = 1.0
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
        return claim

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
        rows = [
            (m.name, spec.to_claim(ts))
            for m in discovery.metrics
            for spec in [
                *derive_claims(m.name, m, discovery.histograms),
                *self.packs.claims_for(m.name),
            ]
        ]
        changed = self.catalog.put_claims_bulk(source, rows)
        summary = {
            "source": source,
            "metrics": len(names),
            "new": len(diff.new),
            "removed": len(diff.removed),
            "returned": len(diff.returned),
            "claims_changed": changed,
            "complete": complete,
            "caveats": list(discovery.caveats),
        }
        self.log.append(actor, "catalog.learned", None, summary)
        return summary

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
        return check_view(view, value_stats(result.buckets, meta.representation, meta.n_min))

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
