"""The lesson scope guard (spec 2026-10-04 R5): a lesson is never broader than its evidence.

An evidence item (a finding or a panel) is read into the label matchers its series satisfy, per
alternative, with `core.claim_scope.read_expr` (the reading that checks finding scopes). The
lesson's scope pins labels: `service` pins every service label (read as aliases of one service
identity), `labels` pin themselves. An alternative covers the lesson when every matcher it
places on an entity label is satisfied by the value the lesson pins there (and the lesson pins
one); non-entity labels (`code`, `le`) select a signal, not a population, and never restrict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from telemetry_nerd.core.claim_scope import Matcher, leaf_matchers, read_expr
from telemetry_nerd.retro.models import SCOPE_LABELS, SERVICE_LABELS, LessonScope, ScopeCheck

_ENTITY = SERVICE_LABELS | SCOPE_LABELS
#: regexes that keep every non-empty value: they do not narrow the population
_KEEP_ALL = frozenset({".*", ".+"})


@dataclass(frozen=True)
class EvidenceScope:
    """What one evidence item is about. `alternatives` None: its scope could not be read."""

    id: str
    source: str | None
    alternatives: list[list[Matcher]] | None
    metrics: frozenset[str] = frozenset()
    why: str | None = None  # why it carries nothing (unreadable, rejected, mixed sources)
    notes: list[str] = field(default_factory=list)


def evidence_from_exprs(
    obj_id: str, source: str | None, exprs: list[str | None], why: str | None = None
) -> EvidenceScope:
    """An evidence item from the expressions behind it (a finding's selector, a panel's
    datasets); None stands for an expression that cannot be read (a code output, a filter)."""
    if why is not None:
        return EvidenceScope(obj_id, source, None, why=why)
    alts: list[list[Matcher]] = []
    metrics: set[str] = set()
    unread: list[str] = []
    for expr in exprs:
        if not expr:
            unread.append("a code output or derived dataset")
            continue
        read = read_expr(expr)
        if read.alternatives is None:
            unread.append(f"{expr!r} ({read.why})")
            continue
        alts += read.alternatives
        metrics |= {m.value for m in leaf_matchers(expr) if m.label == "__name__" and m.op == "="}
        metrics |= {m.value for a in read.alternatives for m in a if m.label == "__name__"
                    and m.op == "="}  # fmt: skip
    if not alts:
        return EvidenceScope(
            obj_id, source, None, why="scope undetermined: cannot read " + "; ".join(unread)
        )
    notes = [f"not read: {u}" for u in unread]
    return EvidenceScope(obj_id, source, alts, frozenset(metrics), notes=notes)


def _restricts(m: Matcher) -> bool:
    if m.label not in _ENTITY:
        return False
    if m.op == "!=" and m.value == "":
        return False
    return not (m.op == "=~" and m.value in _KEEP_ALL)


def in_family(metric: str, family: str) -> bool:
    return fnmatchcase(metric, family) if "*" in family else metric.startswith(family)


def _restricting(alt: list[Matcher]) -> tuple[list[Matcher], list[Matcher]]:
    """The alternative's matchers that narrow the population: (on service labels, on the
    other entity labels)."""
    restricting = [m for m in alt if _restricts(m)]
    return (
        [m for m in restricting if m.label in SERVICE_LABELS],
        [m for m in restricting if m.label not in SERVICE_LABELS],
    )


def _covers_alt(scope: LessonScope, alt: list[Matcher]) -> str | None:
    """None when the alternative's series include everything the scope names; else why not."""
    services, others = _restricting(alt)
    if services:
        if scope.service is None:
            return f"is about {', '.join(map(str, services))} only, not the whole source"
        if not any(m.matches(scope.service) for m in services):
            return f"is about {', '.join(map(str, services))}, not service {scope.service}"
    for m in others:
        pinned = scope.labels.get(m.label)
        if pinned is None:
            return f"is about {m} only: add {m.label} to the lesson's scope labels"
        if not m.matches(pinned):
            return f'is about {m}, not {m.label}="{pinned}"'
    return None


def _unrelated(scope: LessonScope, ev: EvidenceScope) -> str | None:
    """Why `ev` says nothing about the scope at all (unreadable, another source); else None."""
    if ev.why is not None:
        return ev.why
    if ev.source != scope.source:
        return f"is on source {ev.source!r}, the lesson on {scope.source!r}"
    return None


def why_each(whys: dict[str, str | None]) -> str:
    """`{id: why}` as one refusal detail: "f1 is about ...; p2 is on source ..."."""
    return "; ".join(f"{i} {w}" for i, w in whys.items())


def covers(scope: LessonScope, ev: EvidenceScope) -> str | None:
    """None when `ev` carries the whole lesson scope; else why not (for the refusal)."""
    if (why := _unrelated(scope, ev)) is not None:
        return why
    assert ev.alternatives is not None
    if scope.metric_family and not any(in_family(m, scope.metric_family) for m in ev.metrics):
        named = ", ".join(sorted(ev.metrics)) or "no metric name it can read"
        return f"is about {named}, not metric family {scope.metric_family!r}"
    whys = [_covers_alt(scope, alt) for alt in ev.alternatives]
    if any(w is None for w in whys):
        return None
    return whys[0]


def check(scope: LessonScope, items: list[EvidenceScope]) -> ScopeCheck:
    """The guard: at least one item covers the whole scope, else ValueError saying what each
    item is about. Items that do not cover are listed as partial."""
    whys = {ev.id: covers(scope, ev) for ev in items}
    covered = [i for i, w in whys.items() if w is None]
    if not covered:
        raise ValueError(
            f"lesson_beyond_evidence: no evidence covers the scope ({scope.describe()}): "
            f"{why_each(whys)}. Narrow the scope to what the evidence is about (scope.service, "
            "scope.labels), or cite evidence that covers it"
        )
    return ScopeCheck(
        covered_by=covered,
        partial=[{"id": i, "why": w} for i, w in whys.items() if w is not None],
    )


def overlaps(scope: LessonScope, ev: EvidenceScope) -> str | None:
    """None when `ev` is about part of the lesson's scope (so it can refute it); else why not."""
    if (why := _unrelated(scope, ev)) is not None:
        return why
    assert ev.alternatives is not None
    if (
        scope.metric_family
        and ev.metrics
        and not any(in_family(m, scope.metric_family) for m in ev.metrics)
    ):
        return f"is not about metric family {scope.metric_family!r}"
    for alt in ev.alternatives:
        services, others = _restricting(alt)
        if scope.service and services and not any(m.matches(scope.service) for m in services):
            continue
        if all(m.matches(scope.labels[m.label]) for m in others if m.label in scope.labels):
            return None
    return f"is about other entities than the lesson's ({scope.describe()})"
