"""Knowledge packs: curated, versioned facts about well-known exporters (spec §4.2, T0).

A pack is a TOML file of `[[metric]]` entries. Each entry names metrics (exactly, as a list,
or by full-match regex) and asserts only the fields it knows. Applying a pack yields `pack`
claims, which outrank source metadata and name rules but not Claude or the user.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from telemetry_nerd.catalog.models import validate_value
from telemetry_nerd.catalog.relations import validate_bound_params
from telemetry_nerd.catalog.rules import ClaimSpec
from telemetry_nerd.charts.units import metric_names

PACK_CONFIDENCE = 0.85

#: what a metric is *for*; packs are linted against this (catalog roles stay free text)
ROLES = frozenset(
    {
        "cpu-time",
        "load",
        "memory",
        "capacity",
        "saturation",
        "errors",
        "throughput",
        "operations",
        "utilization",
        "latency",
        "state",
        "timestamp",
        "info",
        "config",
        "frequency",
        "temperature",
        "power",
        "count",
    }
)
UNITS = frozenset(
    {"s", "ms", "us", "ns", "B", "B/s", "bit", "count", "ratio", "%", "Hz", "°C", "W", "J"}
)
_VALUE_FIELDS = (
    "type",
    "unit",
    "bounds",
    "additivity_series",
    "additivity_time",
    "role",
    "description",
    "statistic",
)


class PackError(ValueError):
    pass


class BoundSpec(BaseModel):
    """A hard limit (`[[metric.limits]]`) or a threshold (`[[metric.thresholds]]`) drawn on the
    chart of the metric it sits under: how the target metric lines up with it (bead 2as.15)."""

    model_config = ConfigDict(extra="forbid")

    target: str
    expr: str | None = None  # derived target over catalogued metrics, e.g. "quota / period"
    join_on: list[str] | None = None
    matchers: dict[str, str] | None = None
    applies_to: Literal["level", "rate"] = "level"
    zero_is_unlimited: bool = False
    tone: Literal["bad", "warn", "info"] | None = None  # thresholds only
    label: str | None = None

    def params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in ("expr", "join_on", "matchers", "tone", "label"):
            if (v := getattr(self, k)) is not None:
                out[k] = v
        if self.applies_to != "level":
            out["applies_to"] = self.applies_to
        if self.zero_is_unlimited:
            out["zero_is_unlimited"] = True
        return out

    def metrics(self) -> set[str]:
        """Every metric this target needs from the source."""
        return {self.target} | (set(metric_names(self.expr)) if self.expr else set())


class Reframe(BaseModel):
    """`[[reframe]]`: a panel showing `metric` is better shown with `replace_with`."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    replace_with: str
    reason: str
    title: str | None = None


class PackEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    names: list[str] | None = None
    match: str | None = None
    type: str | None = None
    unit: str | None = None
    bounds: str | None = None
    additivity_series: str | None = None
    additivity_time: str | None = None
    role: str | None = None
    description: str | None = None
    statistic: str | None = None
    bounded_by: list[str] | None = None
    limits: list[BoundSpec] | None = None
    thresholds: list[BoundSpec] | None = None

    @model_validator(mode="after")
    def _check(self) -> PackEntry:
        given = [x for x in (self.name, self.names, self.match) if x is not None]
        if len(given) != 1:
            raise ValueError("exactly one of name, names, match is required")
        if self.match is not None:
            try:
                re.compile(self.match)
            except re.error as e:
                raise ValueError(f"invalid match regex {self.match!r}: {e}") from e
        if self.unit is not None and self.unit not in UNITS:
            raise ValueError(
                f"unit {self.unit!r} is not canonical; expected one of {sorted(UNITS)}"
            )
        if self.role is not None and self.role not in ROLES:
            raise ValueError(f"role {self.role!r} not in the role vocabulary {sorted(ROLES)}")
        for f in _VALUE_FIELDS:
            if (v := getattr(self, f)) is not None:
                validate_value(f, v)
        if self.bounded_by is not None and (
            not self.bounded_by or not all(isinstance(t, str) and t for t in self.bounded_by)
        ):
            raise ValueError("bounded_by must be a non-empty list of metric names")
        for spec in self.limits or []:
            if spec.tone is not None:
                raise ValueError("tone belongs on thresholds, not limits")
            validate_bound_params("bounded_by", spec.params())
        for spec in self.thresholds or []:
            validate_bound_params("threshold_by", spec.params())
        return self

    def values(self) -> dict[str, Any]:
        return {f: v for f in _VALUE_FIELDS if (v := getattr(self, f)) is not None}

    def matches(self, metric: str) -> bool:
        if self.name is not None:
            return metric == self.name
        if self.names is not None:
            return metric in self.names
        return re.fullmatch(self.match or "", metric) is not None


class Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    citation: str


@dataclass(frozen=True)
class RelationSpec:
    kind: str
    object: str
    confidence: float
    basis: str
    params: dict[str, Any] = field(default_factory=dict)
    #: every metric the source must have for this relation to apply
    needs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReframeRule:
    metric: str
    replace_with: str
    title: str
    reason: str
    basis: str


@dataclass(frozen=True)
class LoadedPack:
    pack: Pack
    entries: tuple[PackEntry, ...]
    reframes: tuple[Reframe, ...] = ()

    def exact_names(self) -> set[str]:
        out: set[str] = set()
        for e in self.entries:
            out |= set(e.names or ([e.name] if e.name else []))
        return out


def parse_pack(text: str) -> LoadedPack:
    try:
        raw = tomllib.loads(text)
        pack = Pack.model_validate(raw.get("pack", {}))
        entries = tuple(PackEntry.model_validate(m) for m in raw.get("metric", []))
        reframes = tuple(Reframe.model_validate(r) for r in raw.get("reframe", []))
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise PackError(str(e)) from e
    seen: set[str] = set()
    for entry in entries:
        for n in entry.names or ([entry.name] if entry.name else []):
            if n in seen:
                raise PackError(f"pack {pack.name}: duplicate exact entry for {n!r}")
            seen.add(n)
    return LoadedPack(pack, entries, reframes)


@dataclass
class PackIndex:
    packs: tuple[LoadedPack, ...] = field(default_factory=tuple)

    def relations_for(self, metric: str) -> list[RelationSpec]:
        """Relations a pack asserts for `metric`: the `bounded_by` shorthand (metric <= target at
        the same labels), `limits` (hard bounds with optional params) and `thresholds`."""
        out: dict[tuple[str, str], RelationSpec] = {}
        for lp in self.packs:
            cite = f"pack {lp.pack.name}@{lp.pack.version}: {lp.pack.citation}"
            for e in lp.entries:
                if not e.matches(metric):
                    continue
                for target in e.bounded_by or []:
                    out.setdefault(
                        ("bounded_by", target),
                        RelationSpec(
                            "bounded_by", target, PACK_CONFIDENCE, cite, {}, frozenset({target})
                        ),
                    )
                for kind, specs in (("bounded_by", e.limits), ("threshold_by", e.thresholds)):
                    for s in specs or []:
                        out.setdefault(
                            (kind, s.target, s.applies_to, s.label or ""),
                            RelationSpec(
                                kind,
                                s.target,
                                PACK_CONFIDENCE,
                                cite,
                                s.params(),
                                frozenset(s.metrics()),
                            ),
                        )
        return list(out.values())

    def reframes_for(self, metric: str) -> list[ReframeRule]:
        out = []
        for lp in self.packs:
            cite = f"pack {lp.pack.name}@{lp.pack.version}: {lp.pack.citation}"
            out += [
                ReframeRule(
                    r.metric,
                    r.replace_with,
                    r.title or f"show {r.replace_with} instead",
                    r.reason,
                    cite,
                )
                for r in lp.reframes
                if r.metric == metric
            ]
        return out

    def claims_for(self, metric: str) -> list[ClaimSpec]:
        """Pack claims for a metric. Within a pack an exact entry beats a regex entry on the
        same field; across packs the first pack listed wins a field."""
        out: dict[str, ClaimSpec] = {}
        for lp in self.packs:
            cite = f"pack {lp.pack.name}@{lp.pack.version}: {lp.pack.citation}"
            exact: dict[str, Any] = {}
            loose: dict[str, Any] = {}
            for e in lp.entries:
                if not e.matches(metric):
                    continue
                (loose if e.match is not None else exact).update(e.values())
            for f, v in {**loose, **exact}.items():
                out.setdefault(f, ClaimSpec(f, v, "pack", PACK_CONFIDENCE, cite))
        return list(out.values())


BUILTIN = ("node_exporter", "kubernetes", "otel_semconv")


@lru_cache(maxsize=1)
def builtin_packs() -> PackIndex:
    loaded = [
        parse_pack(resources.files(__package__).joinpath(f"packs/{n}.toml").read_text("utf-8"))
        for n in BUILTIN
    ]
    return PackIndex(tuple(loaded))
