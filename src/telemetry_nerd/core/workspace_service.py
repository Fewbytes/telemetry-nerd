"""Reasoning operations. Every mutation appends exactly one event (documented exceptions)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.errors import NotFound
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


@dataclass
class WorkspaceService:
    workspace: WorkspaceStore
    objects: ObjectStore
    datasets: DatasetStore
    log: EventLog

    # annotations --------------------------------------------------------
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

    def delete_annotation(self, annotation_id: str, actor: Actor) -> Annotation:
        a = self.objects.delete_annotation(annotation_id)
        self.log.append(actor, "annotation.deleted", a.id, {})
        return a

    # hypotheses ---------------------------------------------------------
    def hypothesis_create(self, statement: str, actor: Actor) -> Hypothesis:
        h = self.objects.create_hypothesis(statement, actor)
        self.log.append(actor, "hypothesis.created", h.id, {"statement": h.statement})
        return h

    def hypothesis_update(
        self, hypothesis_id: str, status: HypothesisStatus, actor: Actor, note: str | None = None
    ) -> Hypothesis:
        old, h = self.objects.set_hypothesis_status(hypothesis_id, status)
        self.log.append(
            actor, "hypothesis.status_changed", h.id, {"from": old, "to": h.status, "note": note}
        )
        return h

    # findings -----------------------------------------------------------
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
    def ask(
        self,
        text: str,
        actor: Actor,
        anchor: str | None = None,
        selection: TimeSpan | None = None,
    ) -> Thread:
        if not text.strip():
            raise ValueError("message text must not be empty")
        if anchor is not None and anchor.startswith("p"):
            self.workspace.get_panel(anchor)
        t = self.objects.create_thread(anchor, selection, actor)
        m = self.objects.add_message(t.id, text, actor)
        self._message_event(t.id, m, actor, anchor, selection)
        return self.objects.get_thread(t.id)

    def post_message(self, thread_id: str, text: str, actor: Actor) -> Message:
        if not text.strip():
            raise ValueError("message text must not be empty")
        t = self.objects.get_thread(thread_id)
        m = self.objects.add_message(thread_id, text, actor)
        self._message_event(thread_id, m, actor, t.anchor, t.selection)
        return m

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
    def close_panel(self, panel_id: str, actor: Actor) -> Panel:
        p = self.workspace.close_panel(panel_id)
        self.log.append(actor, "panel.closed", p.id, {})
        return p

    def set_focus(self, span: TimeSpan, actor: Actor) -> None:
        self.log.append(
            actor, "focus.changed", None, {"start_ms": span.start_ms, "end_ms": span.end_ms}
        )

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
            "threads": [t.model_dump() for t in self.objects.list_threads()],
            "last_seq": self.log.last_seq,
        }

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
            {"id": p.id, "question": p.question, "status": p.status}
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
        return {
            "events": [
                {
                    "seq": e.seq,
                    "actor": e.actor,
                    "type": e.type,
                    "object_id": e.object_id,
                    "summary": describe_event(e),
                }
                for e in events
            ],
            "last_seq": self.log.last_seq,
        }
