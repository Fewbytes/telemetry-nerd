"""Context lines on a time panel: hard bounds, thresholds and reference series (bead 2as.15).

Pure PromQL text helpers. A context line is another metric (or a derived expression over
metrics) drawn on the panel's chart; these functions turn the panel's own selector plus a relation's
params into the expression that fetches it, and build the reframings that carry context by
themselves. Nothing here talks to a source."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from telemetry_nerd.charts.units import metric_names

MAX_LINES = 6
MAX_REFERENCES = 2


@dataclass(frozen=True)
class ContextSpec:
    """One piece of context resolved from the catalog, before any data is fetched."""

    kind: Literal["limit", "threshold", "reference"]
    target: str  # metric name (or the constant's label)
    origin: str
    confidence: float
    basis: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str | None = None
    tone: Literal["bad", "warn", "info"] | None = None
    value: float | None = None  # a constant threshold: nothing to fetch


_MATCHER = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*("(?:[^"\\]|\\.)*")\s*(?:,|$)')


def parse_matchers(matchers: str) -> list[tuple[str, str, str]]:
    """`{a="b",c=~"d"}` -> [(a, =, "b"), (c, =~, "d")] (value keeps its quotes); [] when empty."""
    body = matchers.strip()
    if not body.startswith("{") or not body.endswith("}"):
        return []
    body = body[1:-1]
    out, pos = [], 0
    while pos < len(body.rstrip()):
        m = _MATCHER.match(body, pos)
        if m is None:
            return []  # something this does not understand: no matchers is the safe reading
        out.append((m.group(1), m.group(2), m.group(3)))
        pos = m.end()
    return out


def line_matchers(panel_matchers: str, params: dict[str, Any]) -> str:
    """Label matchers for a context line: the panel's, narrowed to the labels both sides share
    (`join_on`), plus the relation's own fixed ones (`matchers`)."""
    join_on = params.get("join_on")
    kept = [m for m in parse_matchers(panel_matchers) if join_on is None or m[0] in join_on]
    given = {m[0] for m in kept}
    kept += [
        (k, "=", f'"{v}"') for k, v in (params.get("matchers") or {}).items() if k not in given
    ]
    return "{" + ",".join(f"{k}{op}{v}" for k, op, v in kept) + "}" if kept else ""


def qualify(expr: str, names: set[str], matchers: str) -> str:
    """`a / b` with `{matchers}` attached to each named metric: a derived target under the panel's labels."""
    if not matchers:
        return expr
    pat = re.compile(
        r"(?<![A-Za-z0-9_:])("
        + "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
        + r")(?![A-Za-z0-9_:{(])"
    )
    return pat.sub(lambda m: m.group(1) + matchers, expr)


def line_expr(target: str, params: dict[str, Any], panel_matchers: str) -> str:
    """The PromQL that fetches a bound/threshold/reference target for this panel."""
    matchers = line_matchers(panel_matchers, params)
    expr = params.get("expr")
    if expr:
        out = qualify(expr, metric_names(expr), matchers)
    else:
        out = f"{target}{matchers}"
    # a zero (or, for a cAdvisor quota, -1) means "no limit": the series is dropped, not drawn at 0
    return f"({out}) > 0" if params.get("zero_is_unlimited") else out


def percent_of_limit(panel_expr: str, bound_expr: str, join_on: list[str] | None) -> str:
    """`100 * used / limit`, matched on the labels both share."""
    on = f" on({', '.join(join_on)}) group_left()" if join_on else ""
    return f"100 * ({panel_expr}) /{on} ({bound_expr})"


def headroom(panel_expr: str, bound_expr: str, join_on: list[str] | None) -> str:
    """`limit - used`: how much is left, matched on the labels both share."""
    on = f" on({', '.join(join_on)}) group_right()" if join_on else ""
    return f"({bound_expr}) -{on} ({panel_expr})"


def swap_metric(expr: str, old: str, new: str) -> str | None:
    """`expr` with metric `old` replaced by `new` (selectors keep their matchers); None if absent."""
    pat = re.compile(r"(?<![A-Za-z0-9_:])" + re.escape(old) + r"(?![A-Za-z0-9_:])")
    out, n = pat.subn(new, expr)
    return out if n else None
