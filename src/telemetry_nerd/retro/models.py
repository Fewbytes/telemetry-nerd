"""Lessons and catalog proposals (spec 2026-10-04). Validation of shape lives here; the scope
guard (`retro.guard`) and existence checks live in the service."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from telemetry_nerd.core.entity_ops import KIND_LABELS
from telemetry_nerd.core.evidence_discipline import ENTITY_LABELS
from telemetry_nerd.model.time import parse_duration, parse_time

#: labels whose value names a service: a lesson's `service` pins all of them
SERVICE_LABELS = frozenset(KIND_LABELS["service"])
#: labels a lesson scope may pin through `labels` (a service goes through `service`)
SCOPE_LABELS = (ENTITY_LABELS | frozenset(l for ls in KIND_LABELS.values() for l in ls)) - (
    SERVICE_LABELS
)
DEFAULT_EXPIRY_MS = 180 * 86_400_000
MAX_EXPIRY_MS = 2 * 365 * 86_400_000
MAX_LESSON_TEXT = 500
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EVIDENCE_ID = re.compile(r"^[pf]\d+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LessonScope(_Strict):
    """Where a lesson applies: one source, optionally one service, one metric family and
    entity labels. Never broader than its evidence (retro.guard)."""

    source: str = Field(min_length=1)
    service: str | None = None
    metric_family: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("service", "metric_family")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("must not be blank (leave it out instead)")
        return v.strip() if v is not None else None

    @field_validator("labels")
    @classmethod
    def _labels(cls, v: dict[str, str]) -> dict[str, str]:
        for k, val in v.items():
            if k in SERVICE_LABELS:
                raise ValueError(f"{k!r} names a service: pass it as scope.service")
            if not _LABEL.match(k) or k not in SCOPE_LABELS:
                raise ValueError(
                    f"label {k!r} is not an entity label; scope labels are one of "
                    f"{sorted(SCOPE_LABELS)}"
                )
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"label {k!r} needs a non-empty value")
        return v

    def describe(self) -> str:
        parts = [f"source {self.source}"]
        if self.service:
            parts.append(f"service {self.service}")
        if self.metric_family:
            parts.append(f"metrics {self.metric_family}")
        parts += [f'{k}="{v}"' for k, v in sorted(self.labels.items())]
        return ", ".join(parts)


LessonStatus = Literal["proposed", "approved", "rejected", "refuted"]
#: what a reader sees: `expired` is derived from expires_at_ms, never stored
LessonState = Literal["proposed", "approved", "rejected", "refuted", "expired"]


class ScopeCheck(_Strict):
    """What the guard found: the evidence that carries the whole scope, and the evidence that
    relates to the lesson but covers only part of it (kept, listed)."""

    covered_by: list[str]
    partial: list[dict] = Field(default_factory=list)  # {id, why}


def evidence_ids(v: list[str]) -> list[str]:
    out = list(dict.fromkeys(x.strip() for x in v))
    bad = [x for x in out if not _EVIDENCE_ID.match(x)]
    if bad:
        raise ValueError(f"evidence must be finding or panel ids (f3, p7), got {bad}")
    return out


class Lesson(_Strict):
    id: str
    text: str = Field(min_length=1, max_length=MAX_LESSON_TEXT)
    scope: LessonScope
    evidence: list[str] = Field(min_length=1)
    status: LessonStatus = "proposed"
    author: str
    workspace: str
    created_at_ms: int
    expires_at_ms: int
    decided_at_ms: int | None = None
    comment: str | None = None
    #: the text as Claude proposed it, when the user edited it before approving
    proposed_text: str | None = None
    refuted_by: list[str] = Field(default_factory=list)
    refute_reason: str | None = None
    scope_check: ScopeCheck

    def state(self, now_ms: int) -> LessonState:
        if self.status == "approved" and now_ms >= self.expires_at_ms:
            return "expired"
        return self.status

    def view(self, now_ms: int) -> dict[str, Any]:
        return {**self.model_dump(), "state": self.state(now_ms)}


ProposalStatus = Literal["proposed", "approved", "rejected"]


class CatalogProposal(_Strict):
    id: str
    source: str
    metric: str
    field: str
    value: Any
    confidence: float = Field(gt=0, le=0.9)
    basis: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    status: ProposalStatus = "proposed"
    author: str
    workspace: str
    created_at_ms: int
    decided_at_ms: int | None = None
    #: the value the user approved, when they edited it (written as an origin=user claim)
    decided_value: Any = None
    edited: bool = False
    comment: str | None = None

    @model_validator(mode="after")
    def _evidence(self) -> CatalogProposal:
        self.evidence = evidence_ids(self.evidence)
        return self

    def view(self) -> dict[str, Any]:
        return self.model_dump()


def expires_at(expires: str | None, now_ms: int) -> int:
    """`expires`: a duration from now (`90d`, `26w`) or a date / ISO time; default 180 days;
    at most two years ahead."""
    if expires is None or not str(expires).strip():
        return now_ms + DEFAULT_EXPIRY_MS
    text = str(expires).strip()
    try:
        at = now_ms + parse_duration(text)
    except ValueError:
        try:
            if _DATE.match(text):
                at = int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)
            else:
                at = parse_time(text, now_ms)
        except ValueError as e:
            raise ValueError(
                f"expires {text!r}: a duration such as 90d or a date such as 2027-03-01"
            ) from e
    if at <= now_ms:
        raise ValueError(f"expires {text!r} is not in the future")
    if at - now_ms > MAX_EXPIRY_MS:
        raise ValueError("expires is at most two years ahead: systems change")
    return at
