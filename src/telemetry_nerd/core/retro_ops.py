"""Session retrospective (spec 2026-10-04): Claude proposes catalog updates and scoped lessons,
the user decides; approved lessons surface only where their scope matches."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any, Literal

from telemetry_nerd.catalog.models import Claim, validate_value
from telemetry_nerd.core.events import Actor, check_actor
from telemetry_nerd.core.workspace_service import (
    CLAUDE_MAX_CONFIDENCE,
    WorkspaceService,
    _claude_gate,
    _readable,
)
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.retro.guard import EvidenceScope, check, evidence_from_exprs, overlaps
from telemetry_nerd.retro.models import (
    CatalogProposal,
    Lesson,
    LessonScope,
    evidence_ids,
    expires_at,
)
from telemetry_nerd.retro.store import RetroStore

MAX_PROPOSALS = 50
Decision = Literal["approve", "reject"]


def _scope_key(scope: LessonScope) -> tuple:
    return (scope.source, scope.service, scope.metric_family, tuple(sorted(scope.labels.items())))


class RetroOps:
    def __init__(self, ws: WorkspaceService, workspace_id: Callable[[], str]) -> None:
        self.ws = ws
        self.store = RetroStore(ws.workspace.connection)
        self._workspace_id = workspace_id

    @property
    def _now(self) -> int:
        return self.ws.clock()

    # evidence -----------------------------------------------------------
    def evidence_scope(self, obj_id: str) -> EvidenceScope:
        """What a finding or panel is about (NotFound when there is no such object)."""
        if obj_id.startswith("f"):
            f = self.ws.objects.get_finding(obj_id)
            if f.verdict == "rejected":
                return evidence_from_exprs(obj_id, f.scope.source, [], why="was rejected")
            return evidence_from_exprs(obj_id, f.scope.source, [f.scope.selector])
        panel = self.ws.workspace.get_panel(obj_id)
        metas = []
        for did in panel.dataset_ids:
            try:
                metas.append(self.ws.datasets.meta(did))
            except NotFound:
                continue
        sources = {m.source for m in metas}
        if len(sources) != 1:
            why = "draws no dataset" if not sources else "mixes sources"
            return evidence_from_exprs(obj_id, None, [], why=why)
        return evidence_from_exprs(
            obj_id, sources.pop(), [m.expr if _readable(m) else None for m in metas]
        )

    def _where(self, ids: list[str]) -> dict[str, str | None]:
        """The workspace each evidence object lives in (ids are global)."""
        return {
            i: (self.ws.objects.owner(i) if i.startswith("f") else self.ws.workspace.owner(i))
            for i in ids
        }

    def _check_exist(self, ids: list[str]) -> None:
        missing = [i for i, w in self._where(ids).items() if w is None]
        if missing:
            raise NotFound(f"no such finding or panel: {', '.join(missing)}")

    # catalog proposals --------------------------------------------------
    def catalog_propose(self, source: str, items: list[dict[str, Any]]) -> list[dict]:
        """Claude's catalog proposals, one result per item; bad items are rejected one by one.
        Nothing is written to the catalog until the user approves."""
        if len(items) > MAX_PROPOSALS:
            raise ValueError(f"at most {MAX_PROPOSALS} proposals per call, got {len(items)}")
        open_ = {
            (p.source, p.metric, p.field, repr(p.value)): p.id
            for p in self.store.proposals()
            if p.status == "proposed"
        }
        results = []
        for item in items:
            metric, fld = str(item.get("metric")), str(item.get("field"))
            base = {"metric": metric, "field": fld}
            try:
                confidence, basis = _claude_gate(item, CLAUDE_MAX_CONFIDENCE)
                value = validate_value(fld, item.get("value"))
                if not self.ws.catalog.has_metric(source, metric):
                    raise ValueError(f"unknown metric {metric!r} on {source!r}; run source_learn")
                evidence = evidence_ids(list(item.get("evidence") or []))
                self._check_exist(evidence)
                key = (source, metric, fld, repr(value))
                if key in open_:
                    raise ValueError(f"already proposed as {open_[key]}, awaiting the user")
                win = self.ws.catalog_entry(source, metric).fields.get(fld)
                if (
                    win is not None
                    and win.value == value
                    and (win.origin == "user" or win.verified_by == "user")
                ):
                    raise ValueError("the catalog already holds this, confirmed by the user")
            except (ValueError, NotFound) as e:
                results.append({**base, "status": "rejected", "reason": str(e)})
                continue
            with self.ws.log.transaction():
                p = CatalogProposal(
                    id=self.ws.workspace.next_id("cp"), source=source, metric=metric, field=fld,
                    value=value, confidence=confidence, basis=basis, evidence=evidence,
                    author="claude", workspace=self._workspace_id(), created_at_ms=self._now,
                )  # fmt: skip
                self.store.put_proposal(p)
                self.ws.log.append(
                    "claude", "proposal.created", p.id,
                    {"source": source, "metric": metric, "field": fld, "value": value},
                )  # fmt: skip
            open_[key] = p.id
            results.append({**base, "status": "proposed", "proposal": p.id})
        return results

    def decide_proposal(
        self,
        proposal_id: str,
        decision: Decision,
        actor: Actor = "user",
        value: Any = None,
        comment: str | None = None,
    ) -> CatalogProposal:
        """The user approves (optionally with an edited value) or rejects a proposal. Approval
        writes the claim: origin claude verified_by user, or origin user when edited."""
        check_actor(actor)
        if actor != "user":
            raise ValueError("only the user decides proposals")
        p = self.store.proposal(proposal_id)
        if p.status != "proposed":
            raise ValueError(f"{p.id} is already {p.status}")
        edited = decision == "approve" and value is not None and value != p.value
        with self.ws.log.transaction():
            if decision == "approve":
                if not self.ws.catalog.has_metric(p.source, p.metric):
                    raise NotFound(f"unknown metric {p.metric!r} on {p.source!r}")
                self._approved_claim(p, value if edited else None)
            updated = p.model_copy(
                update={
                    "status": "approved" if decision == "approve" else "rejected",
                    "decided_at_ms": self._now,
                    "decided_value": value if edited else None,
                    "edited": edited,
                    "comment": (comment or "").strip() or None,
                }
            )
            self.store.put_proposal(updated)
            self.ws.log.append(
                actor, "proposal.decided", p.id,
                {"decision": decision, "metric": p.metric, "field": p.field,
                 "value": value if edited else p.value, "edited": edited,
                 **({"comment": updated.comment} if updated.comment else {})},
            )  # fmt: skip
        return updated

    def _approved_claim(self, p: CatalogProposal, edited: Any = None) -> None:
        """The claim an approval writes. Its event is `proposal.decided` (one per mutation)."""
        refs = f"; evidence {', '.join(p.evidence)}" if p.evidence else ""
        if edited is not None:
            validate_value(p.field, edited)
            claim = Claim(
                field=p.field,  # type: ignore[arg-type]
                value=edited, origin="user", confidence=1.0,
                citation=f"edited from proposal {p.id}", ts_ms=self._now,
            )  # fmt: skip
        else:
            claim = Claim(
                field=p.field,  # type: ignore[arg-type]
                value=p.value, origin="claude", confidence=p.confidence, verified_by="user",
                citation=f"{p.basis} (proposal {p.id}{refs})", ts_ms=self._now,
            )  # fmt: skip
        self.ws.catalog.put_claim(p.source, p.metric, claim)
        if p.field in ("unit", "type"):
            self.ws._refresh_panel_units(p.source, p.metric)

    # lessons ------------------------------------------------------------
    def lesson_propose(
        self,
        text: str,
        scope: LessonScope,
        evidence: list[str],
        expires: str | None = None,
        actor: Actor = "claude",
    ) -> Lesson:
        """A methodology lesson, refused unless its evidence covers its whole scope."""
        check_actor(actor)
        text = text.strip()
        if not text:
            raise ValueError("a lesson needs its text")
        ids = evidence_ids(evidence)
        if not ids:
            raise ValueError(
                "lesson_without_evidence: cite the findings (f…) or panels (p…) the lesson "
                "comes from"
            )
        self._check_exist(ids)
        scope_check = check(scope, [self.evidence_scope(i) for i in ids])
        for other in self.store.lessons():
            if (
                other.text.casefold() == text.casefold()
                and _scope_key(other.scope) == _scope_key(scope)
                and other.state(self._now) in ("proposed", "approved")
            ):
                raise ValueError(f"already on file as {other.id} ({other.state(self._now)})")
        now = self._now
        with self.ws.log.transaction():
            lesson = Lesson(
                id=self.ws.workspace.next_id("ls"), text=text, scope=scope, evidence=ids,
                author=actor, workspace=self._workspace_id(), created_at_ms=now,
                expires_at_ms=expires_at(expires, now), scope_check=scope_check,
            )  # fmt: skip
            self.store.put_lesson(lesson)
            self.ws.log.append(
                actor, "lesson.proposed", lesson.id,
                {"text": text, "scope": scope.model_dump(exclude_none=True), "evidence": ids},
            )  # fmt: skip
        return lesson

    def decide_lesson(
        self,
        lesson_id: str,
        decision: Decision,
        actor: Actor = "user",
        text: str | None = None,
        expires: str | None = None,
        comment: str | None = None,
    ) -> Lesson:
        """The user approves (optionally editing text or expiry) or rejects a proposed lesson."""
        check_actor(actor)
        if actor != "user":
            raise ValueError("only the user decides lessons")
        lesson = self.store.lesson(lesson_id)
        if lesson.status != "proposed":
            raise ValueError(f"{lesson.id} is already {lesson.status}")
        update: dict[str, Any] = {
            "status": "approved" if decision == "approve" else "rejected",
            "decided_at_ms": self._now,
            "comment": (comment or "").strip() or None,
        }
        if decision == "approve" and text is not None and text.strip() != lesson.text:
            if not text.strip():
                raise ValueError("a lesson needs its text")
            update |= {"text": text.strip(), "proposed_text": lesson.text}
        if decision == "approve" and expires is not None and expires.strip():
            update["expires_at_ms"] = expires_at(expires, self._now)
        updated = Lesson.model_validate({**lesson.model_dump(), **update})
        with self.ws.log.transaction():
            self.store.put_lesson(updated)
            self.ws.log.append(
                actor, "lesson.decided", lesson.id,
                {"decision": decision, "text": updated.text,
                 "edited": updated.proposed_text is not None,
                 **({"comment": updated.comment} if updated.comment else {})},
            )  # fmt: skip
        return updated

    def refute_lesson(
        self, lesson_id: str, evidence: list[str], reason: str, actor: Actor = "claude"
    ) -> Lesson:
        """Mark an approved lesson refuted. Claude cites a finding whose scope overlaps the
        lesson's; the user's own refutation needs only the reason."""
        check_actor(actor)
        if not reason or not reason.strip():
            raise ValueError("reason is required: what the finding shows against the lesson")
        lesson = self.store.lesson(lesson_id)
        if lesson.status != "approved":
            raise ValueError(
                f"{lesson.id} is {lesson.status}: only an approved lesson is refuted "
                "(a proposed one is rejected)"
            )
        ids = evidence_ids(evidence)
        if any(not i.startswith("f") for i in ids):
            raise ValueError("a lesson is refuted by findings (f…), not panels")
        self._check_exist(ids)
        if actor != "user":
            if not ids:
                raise ValueError("cite the finding (f…) that contradicts the lesson")
            whys = {i: overlaps(lesson.scope, self.evidence_scope(i)) for i in ids}
            if all(w is not None for w in whys.values()):
                detail = "; ".join(f"{i} {w}" for i, w in whys.items())
                raise ValueError(
                    f"no cited finding is about the lesson's scope ({lesson.scope.describe()}): "
                    f"{detail}"
                )
        updated = lesson.model_copy(
            update={"status": "refuted", "refuted_by": ids, "refute_reason": reason.strip()}
        )
        with self.ws.log.transaction():
            self.store.put_lesson(updated)
            self.ws.log.append(
                actor, "lesson.refuted", lesson.id,
                {"text": lesson.text, "evidence": ids, "reason": reason.strip()},
            )  # fmt: skip
        return updated

    # reading ------------------------------------------------------------
    def lessons_for(
        self,
        source: str,
        services: list[str] | None = None,
        metric_families: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict:
        """Approved, unexpired lessons whose whole scope matches; `held` counts the approved
        lessons on the source whose narrower scope the call did not name."""
        services_ = {s.strip() for s in services or [] if s.strip()}
        families = {f.strip() for f in metric_families or [] if f.strip()}
        labels_ = labels or {}
        now = self._now
        hit, held = [], 0
        for lesson in self.store.lessons():
            if lesson.state(now) != "approved" or lesson.scope.source != source:
                continue
            s = lesson.scope
            if (
                (s.service is None or s.service in services_)
                and (s.metric_family is None or s.metric_family in families)
                and all(labels_.get(k) == v for k, v in s.labels.items())
            ):
                hit.append(lesson)
            else:
                held += 1
        return {
            "source": source,
            "lessons": [self._lesson_row(lesson) for lesson in hit],
            "held": held,
        }

    def _lesson_row(self, lesson: Lesson) -> dict:
        return {
            "id": lesson.id,
            "text": lesson.text,
            "scope": lesson.scope.model_dump(exclude_none=True, exclude_defaults=True)
            | {"source": lesson.scope.source},
            "evidence": [
                {"id": i, "workspace": w} for i, w in self._where(lesson.evidence).items()
            ],
            "expires_at_ms": lesson.expires_at_ms,
        }

    def listing(self, status: str | None = None) -> dict:
        """Every proposal and lesson (newest first), each with the workspace of its evidence."""
        now = self._now
        lessons = [
            {**lesson.view(now), "evidence_where": self._where(lesson.evidence)}
            for lesson in reversed(self.store.lessons())
        ]
        catalog = [
            {**p.view(), "evidence_where": self._where(p.evidence)}
            for p in reversed(self.store.proposals())
        ]
        if status is not None:
            lessons = [x for x in lessons if x["state"] == status]
            catalog = [x for x in catalog if x["status"] == status]
        return {
            "catalog": catalog,
            "lessons": lessons,
            "pending": sum(x["status"] == "proposed" for x in catalog)
            + sum(x["state"] == "proposed" for x in lessons),
        }

    def summary(self, connected: set[str] | None = None) -> dict:
        """For the SessionStart hook: approved lesson counts per connected source (never their
        text) and how many proposals await review."""
        now = self._now
        lessons = self.store.lessons()
        per_source = Counter(
            x.scope.source
            for x in lessons
            if x.state(now) == "approved" and (connected is None or x.scope.source in connected)
        )
        pending = sum(x.status == "proposed" for x in lessons) + sum(
            p.status == "proposed" for p in self.store.proposals()
        )
        return {"lessons": dict(sorted(per_source.items())), "pending": pending}
