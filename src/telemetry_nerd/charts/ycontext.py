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
    """(metric, `{matchers}` or "") when the expression is rate/irate/increase of one plain selector."""
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
