"""Catalog claims and resolution.

A field's value is never stored bare: each origin keeps its own claim and the winner is
computed on read, so a lower-ranked claim that contradicts the winner stays visible.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

from telemetry_nerd.model.discovery import MetricType

Origin = Literal["user", "claude", "stats", "context", "pack", "metadata", "rule"]
#: precedence: user > claude > stats > context > pack > metadata > rule. `context` is what the
#: repo's code, docs and dashboards say: more specific than a generic pack, but a declaration that
#: may be stale, so observed behaviour (stats) outranks it.
ORIGIN_RANK: dict[str, int] = {
    "rule": 0,
    "metadata": 1,
    "pack": 2,
    "context": 3,
    "stats": 4,
    "claude": 5,
    "user": 6,
}
ORIGINS = frozenset(get_args(Origin))

FieldName = Literal[
    "type",
    "unit",
    "bounds",
    "additivity_series",
    "additivity_time",
    "role",
    "description",
    "histogram_family",
    "operating_profile_ref",
    "thresholds",
    "statistic",
    "typical_range",
]
FIELDS = frozenset(get_args(FieldName))

PROSE_FIELDS = frozenset({"description"})
_METRIC_TYPES = frozenset(get_args(MetricType))
_BOUNDS = frozenset({"≥0", "[0,1]", "[0,100]", "none"})
_ADDITIVITY = frozenset({"additive", "intensive", "none"})
_TEXT = frozenset({"unit", "role", "description", "operating_profile_ref"})
#: what kind of statistic a metric's value IS (spec §5/[SfE] mergeability table): drives
#: whether cross-time/cross-series aggregation is meaningful (see catalog.mergeability)
_STATISTIC = frozenset(
    {
        "count",
        "count_below",
        "sum",
        "min",
        "max",
        "mean",
        "ratio",
        "median",
        "percentile",
        "truncated_mean",
        "mad",
        "iqr",
    }
)


THRESHOLD_TONES = ("bad", "warn", "info")


def _validate_thresholds(value: Any) -> None:
    """Known good/bad lines for a metric (an SLO, a renewal window): drawn on its chart."""
    if not isinstance(value, list) or not value or len(value) > 8:
        raise ValueError("thresholds must be a list of 1 to 8 {value, label, tone} objects")
    for t in value:
        ok = (
            isinstance(t, dict)
            and set(t) <= {"value", "label", "tone", "direction"}
            and isinstance(t.get("value"), int | float)
            and not isinstance(t.get("value"), bool)
            and math.isfinite(t["value"])
            and isinstance(t.get("label"), str)
            and 0 < len(t["label"].strip()) <= 80
            and t.get("tone", "info") in THRESHOLD_TONES
            and t.get("direction", "above") in ("above", "below")
        )
        if not ok:
            raise ValueError(
                "each threshold needs a finite `value`, a short `label`, `tone` "
                "bad|warn|info and optionally `direction` above|below"
            )


def _validate_typical_range(value: Any) -> None:
    """An observed characteristic range {lo, hi, window, n, ...}: descriptive, not a bound."""
    ok = (
        isinstance(value, dict)
        and all(
            isinstance(value.get(k), int | float)
            and not isinstance(value.get(k), bool)
            and math.isfinite(value[k])
            for k in ("lo", "hi", "n")
        )
        and value["lo"] <= value["hi"]
        and value["n"] > 0
        and isinstance(value.get("window"), str)
    )
    if not ok:
        raise ValueError(
            "typical_range must be {lo, hi, n, window, ...} with finite lo <= hi and n > 0"
        )


def validate_value(field: str, value: Any) -> Any:
    """Return the value if it is valid for the field, else raise ValueError."""
    if field not in FIELDS:
        raise ValueError(f"unknown catalog field {field!r}; expected one of {sorted(FIELDS)}")
    enum = {
        "type": _METRIC_TYPES,
        "bounds": _BOUNDS,
        "additivity_series": _ADDITIVITY,
        "additivity_time": _ADDITIVITY,
        "statistic": _STATISTIC,
    }.get(field)
    if enum is not None:
        if value not in enum:
            raise ValueError(f"invalid {field} {value!r}; expected one of {sorted(enum)}")
    elif field in _TEXT:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
    elif field == "thresholds":
        _validate_thresholds(value)
    elif field == "typical_range":
        _validate_typical_range(value)
    elif field == "histogram_family" and (
        not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value)
    ):
        raise ValueError("histogram_family must be a non-empty list of metric names")
    return value


def native_family(members: Iterable[str]) -> bool:
    """A `histogram_family` claim's members describe a native histogram: no `_bucket` series."""
    return not any(m.endswith("_bucket") for m in members)


class Claim(BaseModel):
    field: FieldName
    value: Any
    origin: Origin
    confidence: float = Field(ge=0.0, le=1.0)
    verified_by: str | None = None
    #: where this came from: file:line, URL, rule id, ...
    citation: str | None = None
    ts_ms: int


def _rank(c: Claim) -> tuple[int, float, int, str]:
    # value is the last tiebreak so resolution is independent of input order
    return (ORIGIN_RANK[c.origin], c.confidence, c.ts_ms, json.dumps(c.value, sort_keys=True))


def resolve(claims: Iterable[Claim]) -> Claim | None:
    """Winner: highest origin rank, then confidence, then recency."""
    return max(claims, key=_rank, default=None)


def ordered(claims: Iterable[Claim]) -> list[Claim]:
    """Winner first."""
    return sorted(claims, key=_rank, reverse=True)


class CatalogEntry(BaseModel):
    source: str
    metric: str
    #: False once a re-learn no longer sees the metric (claims are kept)
    present: bool
    first_seen_ms: int
    last_seen_ms: int
    #: resolved value per field
    fields: dict[str, Claim]
    #: every claim per field, winner first
    claims: dict[str, list[Claim]]
    #: name-template family this metric belongs to (and the dimension its name encodes)
    family: str | None = None
    dimension: str | None = None
    #: this entry IS a family: its template, with the number of member metrics
    is_family: bool = False
    family_members: int | None = None
    #: claims shown are the family's (the member has none of its own)
    inherited_from: str | None = None

    def conflicts(self) -> dict[str, list[Claim]]:
        """Fields where a lower-ranked claim disagrees with the winner.

        Descriptions are prose: two origins wording the same thing differently is not a
        contradiction, so they are never reported."""
        return {
            f: [c for c in cs[1:] if c.value != cs[0].value]
            for f, cs in self.claims.items()
            if f not in PROSE_FIELDS and any(c.value != cs[0].value for c in cs[1:])
        }


class RelearnDiff(BaseModel):
    new: list[str]
    removed: list[str]
    returned: list[str]
