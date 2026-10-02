"""T0 rules: deterministic claims from source metadata and naming conventions (spec §4.2).

Two origins come out of here. `metadata` is what the source declared (TYPE/HELP/UNIT); `rule`
is what the name conventions imply. Rules only claim what the convention reliably says: a plain
`_seconds` or `_bytes` metric may be a gauge or a counter, so no type is guessed for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from telemetry_nerd.catalog.models import Claim, Origin, resolve
from telemetry_nerd.model.discovery import MetricInfo

#: how an origin is described next to a unit/type on a chart or in the metric card
PROVENANCE: dict[str, str] = {
    "user": "set by user",
    "claude": "provided by claude",
    "stats": "measured from data",
    "context": "repo, docs or dashboard",
    "pack": "knowledge pack",
    "metadata": "source metadata",
    "rule": "inferred from metric name",
}

# declared unit strings -> canonical; "1" (UCUM dimensionless) says nothing, so it is omitted
_DECLARED_UNITS = {
    "s": "s",
    "second": "s",
    "seconds": "s",
    "ms": "ms",
    "millisecond": "ms",
    "milliseconds": "ms",
    "us": "us",
    "microseconds": "us",
    "ns": "ns",
    "nanoseconds": "ns",
    "by": "B",
    "byte": "B",
    "bytes": "B",
    "hz": "Hz",
    "w": "W",
    "j": "J",
    "cel": "°C",
    "ratio": "ratio",
    "percent": "%",
    "%": "%",
}

_SUFFIX_UNITS = (
    ("_seconds", "s"),
    ("_milliseconds", "ms"),
    ("_microseconds", "us"),
    ("_nanoseconds", "ns"),
    ("_bytes", "B"),
    ("_hertz", "Hz"),
    ("_watts", "W"),
    ("_joules", "J"),
    ("_volts", "V"),
    ("_amperes", "A"),
    ("_celsius", "°C"),
    ("_ratio", "ratio"),
    ("_percent", "%"),
)
_MEMBERS = ("_bucket", "_sum", "_count")
#: Prometheus percentile-gauge convention: `..._p99`, `..._p95`, `..._p999` (three digits for
#: p99.9). These carry a pre-computed quantile exactly like a summary's `quantile=` series and
#: must never be aggregated across time or instances (spec §5 [H], rule 4).
_PERCENTILE_SUFFIX = re.compile(r"(?:^|_)p[0-9]{1,3}$")


@dataclass(frozen=True)
class ClaimSpec:
    field: str
    value: object
    origin: Origin
    confidence: float
    citation: str | None = None

    def to_claim(self, ts_ms: int) -> Claim:
        return Claim(
            field=self.field,  # type: ignore[arg-type]
            value=self.value,
            origin=self.origin,
            confidence=self.confidence,
            citation=self.citation,
            ts_ms=ts_ms,
        )


@dataclass(frozen=True)
class Facts:
    """What a chart needs to know about a metric, and who vouches for it."""

    unit: str | None
    type: str | None
    unit_provenance: str | None
    #: mergeability table kind (catalog.mergeability): drives aggregation guards
    statistic: str | None = None


def normalize_unit(declared: str | None) -> str | None:
    if declared is None:
        return None
    return _DECLARED_UNITS.get(declared.strip().lower())


def _name_unit(n: str) -> tuple[str | None, str | None]:
    """(unit, rule id) from the name suffixes; `n` has dots already turned into underscores."""
    total = n.endswith("_total")
    base = n.removesuffix("_total")
    if base.endswith("_count"):
        return "count", "name:_count"  # observations, not the base unit
    for member in ("_sum", "_bucket"):
        if base.endswith(member):
            base = base.removesuffix(member)
            break
    for suffix, unit in _SUFFIX_UNITS:
        if base.endswith(suffix):
            return unit, f"name:{suffix}"
    return ("count", "name:_total") if total else (None, None)


def derive_claims(
    name: str,
    info: MetricInfo | None = None,
    families: Mapping[str, str] | None = None,
) -> list[ClaimSpec]:
    """Claims for one metric. `families` maps histogram base name -> "classic" | "native"."""
    out: list[ClaimSpec] = []
    families = families or {}

    if info is not None:
        if info.type is not None:
            out.append(ClaimSpec("type", info.type, "metadata", 0.9, "declared TYPE"))
        if (unit := normalize_unit(info.unit)) is not None:
            out.append(ClaimSpec("unit", unit, "metadata", 0.9, f"declared UNIT {info.unit!r}"))
        if info.help and info.help.strip():
            out.append(
                ClaimSpec("description", info.help.strip(), "metadata", 0.9, "declared HELP")
            )

    n = name.replace(".", "_")
    counter = False
    unit, rule = _name_unit(n)
    if name == "traces_spanmetrics_latency":  # Tempo/OTel span metrics: seconds, no unit suffix
        unit, rule = "s", "span-metrics convention"
        out.append(ClaimSpec("type", "histogram", "rule", 0.8, rule))
    if unit is not None:
        out.append(ClaimSpec("unit", unit, "rule", 0.5 if rule == "name:_total" else 0.7, rule))

    if n.endswith("_total"):
        counter = True
        out.append(ClaimSpec("type", "counter", "rule", 0.8, "name:_total"))
    elif n.endswith("_info"):
        # no type claim: Prometheus exposes *_info as a gauge (value 1), and claiming "info"
        # contradicted the correct declaration on every one of them (Grafana Play run)
        out.append(ClaimSpec("additivity_series", "none", "rule", 0.8, "name:_info"))

    # statistic kind (spec §5 [H]/[SfE] mergeability table): a Prometheus summary's base series
    # carries the `quantile=` label (never aggregatable); its _sum/_count members are themselves
    # mergeable (sum, count) same as a classic histogram's.
    if info is not None and info.type == "summary" and not n.endswith(("_sum", "_count")):
        out.append(
            ClaimSpec(
                "statistic", "percentile", "rule", 0.8, "summary base series (quantile label)"
            )
        )
    elif n.endswith("_sum"):
        out.append(ClaimSpec("statistic", "sum", "rule", 0.5, "name:_sum"))
    elif n.endswith("_count"):
        out.append(ClaimSpec("statistic", "count", "rule", 0.5, "name:_count"))
    elif _PERCENTILE_SUFFIX.search(n):
        # exported p99-style gauge (spec §5 [H] rule 4): pre-computed, never aggregatable
        out.append(
            ClaimSpec("statistic", "percentile", "rule", 0.6, "name:_pNN percentile convention")
        )

    base = next((n.removesuffix(m) for m in _MEMBERS if n.endswith(m)), None)
    kind = families.get(name) or (families.get(base) if base else None)
    fam_base = name if name in families else base
    if kind == "classic" and base is not None and base in families:
        counter = True
        out.append(ClaimSpec("type", "counter", "rule", 0.7, f"member of histogram {base}"))
    if kind is not None and fam_base is not None:
        members = [f"{fam_base}{m}" for m in _MEMBERS] if kind == "classic" else [fam_base]
        why = (
            "classic histogram: _bucket/_sum/_count series"
            if kind == "classic"
            else "metadata histogram without _bucket series (native)"
        )
        out.append(
            ClaimSpec("histogram_family", members, "rule", 0.95 if kind == "classic" else 0.9, why)
        )

    if unit in ("s", "ms", "us", "ns", "B", "Hz", "W", "J", "count"):
        out.append(ClaimSpec("bounds", "≥0", "rule", 0.7, rule))
    elif unit == "ratio":
        out.append(ClaimSpec("bounds", "[0,1]", "rule", 0.6, rule))
    elif unit == "%":
        # CPU percent across cores can exceed 100, so only non-negativity is claimed
        out.append(ClaimSpec("bounds", "≥0", "rule", 0.4, rule))

    if counter:
        out.append(ClaimSpec("additivity_series", "additive", "rule", 0.8, "counter"))
        out.append(ClaimSpec("additivity_time", "additive", "rule", 0.8, "counter (increase)"))
    elif unit in ("ratio", "%"):
        out.append(ClaimSpec("additivity_series", "intensive", "rule", 0.7, rule))
        out.append(ClaimSpec("additivity_time", "intensive", "rule", 0.7, rule))
    elif unit == "B" and not n.endswith("_info"):
        out.append(ClaimSpec("additivity_series", "additive", "rule", 0.5, rule))
    return out


def facts_from_claims(claims: list[Claim]) -> Facts:
    """Resolve the unit and type winners among already-stored claims."""
    unit = resolve(c for c in claims if c.field == "unit")
    kind = resolve(c for c in claims if c.field == "type")
    statistic = resolve(c for c in claims if c.field == "statistic")
    return Facts(
        unit.value if unit else None,
        kind.value if kind else None,
        PROVENANCE[unit.origin] if unit else None,
        statistic.value if statistic else None,
    )


def facts_from_name(name: str) -> Facts:
    """Rule-only facts: what a chart gets for a metric the catalog has never learned."""
    return facts_from_claims([c.to_claim(0) for c in derive_claims(name)])
