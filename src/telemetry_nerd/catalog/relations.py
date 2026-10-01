"""Relations (typed edges) and model bindings (role-based, n-ary) between metrics or datasets.

Both are claims per origin, like catalog fields: the highest-ranked origin decides, and a claim
may be a retraction ("this does not hold"), so a wrong pack edge can be removed without erasing
history. Catalog level relates a source's metrics; workspace level relates specific datasets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

from telemetry_nerd.catalog.models import ORIGIN_RANK, Origin

Level = Literal["catalog", "workspace"]
RelationKind = Literal[
    "derived_from", "part_of", "same_quantity", "upstream_of", "bounded_by", "correlated"
]
RELATION_KINDS = frozenset(get_args(RelationKind))
#: endpoint order carries no meaning, so (a, b) and (b, a) are one edge
SYMMETRIC = frozenset({"same_quantity", "correlated"})
#: empirical association is evidence, never truth: Claude may not be more than this sure of it
CORRELATED_MAX_CONFIDENCE = 0.7

BINDING_ROLES: dict[str, tuple[str, ...]] = {
    "littles_law": ("arrival_rate", "latency", "concurrency"),
    "RED": ("rate", "errors", "duration"),
    "USE": ("utilization", "saturation", "errors"),
}
BindingKind = Literal["littles_law", "RED", "USE"]
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Suggestion:
    """Instrumentation to recommend when a binding role has no signal."""

    name: str  # may contain {key}
    type: Literal["counter", "gauge", "histogram", "summary"]
    labels: tuple[str, ...]
    why: str


SUGGESTIONS: dict[tuple[str, str], Suggestion] = {
    ("littles_law", "arrival_rate"): Suggestion(
        "{key}_requests_total", "counter", ("service", "route"), "arrivals per second λ"
    ),
    ("littles_law", "latency"): Suggestion(
        "{key}_request_duration_seconds", "histogram", ("service", "route"), "time in system W"
    ),
    ("littles_law", "concurrency"): Suggestion(
        "{key}_active_requests", "gauge", ("service", "route"), "requests in flight L"
    ),
    ("RED", "rate"): Suggestion(
        "{key}_requests_total", "counter", ("service", "route"), "request rate"
    ),
    ("RED", "errors"): Suggestion(
        "{key}_request_errors_total", "counter", ("service", "route"), "failed requests"
    ),
    ("RED", "duration"): Suggestion(
        "{key}_request_duration_seconds", "histogram", ("service", "route"), "latency distribution"
    ),
    ("USE", "utilization"): Suggestion(
        "{key}_utilization_ratio", "gauge", ("instance",), "fraction of capacity in use"
    ),
    ("USE", "saturation"): Suggestion(
        "{key}_queue_depth", "gauge", ("instance",), "work waiting for the resource"
    ),
    ("USE", "errors"): Suggestion(
        "{key}_errors_total", "counter", ("instance",), "resource errors"
    ),
}


def metric_slug(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", key).strip("_").lower() or "service"


def _tiebreak(c: BaseModel) -> str:
    return json.dumps(c.model_dump(mode="json"), sort_keys=True)


def winner[C: BaseModel](claims: Iterable[C]) -> C | None:
    """Highest origin rank, then confidence, then recency; fully order-independent."""
    return max(
        claims,
        key=lambda c: (
            ORIGIN_RANK[c.origin],  # type: ignore[attr-defined]
            c.confidence,  # type: ignore[attr-defined]
            c.ts_ms,  # type: ignore[attr-defined]
            _tiebreak(c),
        ),
        default=None,
    )


class RelationClaim(BaseModel):
    level: Level = "catalog"
    source: str = ""
    subject: str
    kind: RelationKind
    object: str
    origin: Origin
    confidence: float = Field(ge=0.0, le=1.0)
    retracted: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    basis: str | None = None
    ts_ms: int


class BindingClaim(BaseModel):
    level: Level = "catalog"
    source: str = ""
    kind: BindingKind
    key: str
    origin: Origin
    confidence: float = Field(ge=0.0, le=1.0)
    retracted: bool = False
    #: every role of the kind; None = no signal (raises a Gap)
    roles: dict[str, str | None]
    join_on: list[str] = Field(default_factory=list)
    basis: str | None = None
    ts_ms: int


class ResolvedRelation(BaseModel):
    subject: str
    kind: RelationKind
    object: str
    winner: RelationClaim
    #: a lower-ranked claim disagrees (asserts what the winner retracts, or the reverse)
    contested: bool
    claims: list[RelationClaim]


class ResolvedBinding(BaseModel):
    kind: BindingKind
    key: str
    winner: BindingClaim
    contested: bool
    claims: list[BindingClaim]


def canonical_ends(kind: str, subject: str, obj: str) -> tuple[str, str]:
    return (min(subject, obj), max(subject, obj)) if kind in SYMMETRIC else (subject, obj)


def validate_relation(kind: str, subject: str, obj: str, params: Mapping[str, Any]) -> None:
    if kind not in RELATION_KINDS:
        raise ValueError(
            f"unknown relation kind {kind!r}; expected one of {sorted(RELATION_KINDS)}"
        )
    if subject == obj:
        raise ValueError("a relation needs two different endpoints")
    if kind == "correlated":
        coef, lag, scope = params.get("coefficient"), params.get("lag_ms"), params.get("scope")
        if not isinstance(coef, int | float) or not -1 <= coef <= 1:
            raise ValueError("correlated needs params.coefficient in [-1, 1]")
        if not isinstance(lag, int) or isinstance(lag, bool):
            raise ValueError("correlated needs integer params.lag_ms")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError(
                "correlated needs params.scope: where/when the association was measured"
            )
    elif params:
        raise ValueError(f"{kind} takes no params, got {sorted(params)}")


def validate_binding(kind: str, roles: Mapping[str, str | None], join_on: Iterable[str]) -> None:
    if kind not in BINDING_ROLES:
        raise ValueError(f"unknown binding kind {kind!r}; expected one of {sorted(BINDING_ROLES)}")
    expected = set(BINDING_ROLES[kind])
    if set(roles) != expected:
        raise ValueError(
            f"{kind} needs exactly the roles {sorted(expected)} (null for a missing signal), "
            f"got {sorted(roles)}"
        )
    bad = [lbl for lbl in join_on if not _LABEL.fullmatch(lbl)]
    if bad:
        raise ValueError(f"join_on has invalid label names: {bad}")
