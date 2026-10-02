"""What the catalog says about a panel's y axis (spec §6.2, bead 2as.10). Pure: no I/O."""

from __future__ import annotations

import re

#: bounds claim -> (natural lower, natural upper); None = unbounded on that side
NATURAL: dict[str, tuple[float | None, float | None]] = {
    "≥0": (0.0, None),
    "[0,1]": (0.0, 1.0),
    "[0,100]": (0.0, 100.0),
    "none": (None, None),
}
_SELECTOR = re.compile(r"\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^{}]*\})?\s*", re.DOTALL)
_RATE = re.compile(
    r"\s*(?:rate|irate|increase)\(\s*([a-zA-Z_:][a-zA-Z0-9_:]*\s*(?:\{[^{}]*\})?)\s*\[[^\]]+\]\s*\)\s*"
)


def selector_parts(expr: str) -> tuple[str, str] | None:
    """(metric, `{matchers}` or "") when the whole expression is one plain selector."""
    m = _SELECTOR.fullmatch(expr)
    return (m.group(1), m.group(2) or "") if m else None


def counter_rate_parts(expr: str) -> tuple[str, str] | None:
    """(metric, `{matchers}` or "") when the expression is rate/irate/increase of one selector."""
    m = _RATE.fullmatch(expr)
    return selector_parts(m.group(1)) if m else None


def counter_rate_metric(expr: str) -> str | None:
    """The metric when the expression is rate/irate/increase of one plain selector."""
    parts = counter_rate_parts(expr)
    return parts[0] if parts else None


def natural_range(bounds: str | None) -> tuple[float | None, float | None]:
    return NATURAL.get(bounds or "", (None, None))


def limit_expr(matchers: str, target: str) -> str:
    """The bounding metric under the same label matchers as the panel's selector."""
    return f"{target}{matchers}"


#: a counter's *rate* can obey a ceiling its own raw value does not (bead 2as.15): a byte
#: counter is unbounded over time, but bytes/sec cannot exceed the link speed; CPU-seconds/sec
#: cannot exceed the CFS quota/period. These are rate-specific facts (origin "rule"), distinct
#: from a `bounded_by` relation on the counter itself (which describes the raw running total).
#: metric -> (limit label, expr template with `{matchers}`, basis)
RATE_BOUNDS: dict[str, tuple[str, str, str]] = {
    "node_network_receive_bytes_total": (
        "node_network_speed_bytes",
        "node_network_speed_bytes{matchers}",
        "link speed ceiling on the interface's throughput",
    ),
    "node_network_transmit_bytes_total": (
        "node_network_speed_bytes",
        "node_network_speed_bytes{matchers}",
        "link speed ceiling on the interface's throughput",
    ),
    "container_cpu_usage_seconds_total": (
        "container_spec_cpu_quota/container_spec_cpu_period",
        "(container_spec_cpu_quota{matchers} / container_spec_cpu_period{matchers})",
        "CFS quota/period: the container's CPU limit in cores",
    ),
}
RATE_BOUND_CONFIDENCE = 0.75  # a rule, not a direct relation claim: less certain than a pack edge


def rate_bound(metric: str, matchers: str) -> tuple[str, str, str] | None:
    """(limit label, expr, basis) for the ceiling of `metric`'s rate, when a rule knows one."""
    found = RATE_BOUNDS.get(metric)
    if found is None:
        return None
    label, template, basis = found
    return label, template.format(matchers=matchers), basis


def reframing_specs(
    expr: str, limit_fetch_expr: str, limit_label: str
) -> list[tuple[str, str, str, str]]:
    """(transform, expr, label, reason) for transforms that would carry a resolved bound with
    them, instead of a separate limit line (bead 2as.15). Always proposed, never applied for you;
    the caller shows them as a suggestion, not a silent substitution."""
    return [
        (
            "headroom",
            f"({limit_fetch_expr}) - ({expr})",
            "headroom (limit − value)",
            f"the distance to {limit_label} carries the bound with it; no separate limit line needed",
        ),
        (
            "percent_of_limit",
            f"100 * ({expr}) / ({limit_fetch_expr})",
            "% of limit",
            f"a percentage of {limit_label} means the same risk at any scale",
        ),
    ]  # fmt: skip
