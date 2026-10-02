"""Natural bounds carried by derived expressions (bead f2z). Pure: no I/O.

A small, conservative rule library: an expression gets bounds only when it matches one of the
patterns below; everything else is unbounded (as before). Catalog facts reach it through callbacks.

* `rate(<fraction-of-time counter>)` is in [0,1] per series (node_cpu_seconds_total: the modes of
  a core partition its time); `avg/min/max` of it stays in [0,1]; `sum` does not.
* `1 - X` for X in [0,1] is in [0,1].
* `A / B` is in [0,1] when A is demonstrably a part of B: the same metric under extra label
  matchers, a catalog `bounded_by` relation (level values), or an error-ish metric over a
  sibling total, with the same aggregation, the same function and compatible units.
* `100 * X` (or `X * 100`) for X in [0,1] is in [0,100], in percent.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from telemetry_nerd.charts.ycontext import natural_range, selector_parts

#: counters whose per-series rate is a fraction of wall time: each series is in [0,1]
FRACTION_OF_TIME = {"node_cpu_seconds_total", "node_disk_io_time_seconds_total"}
CONF_TIME, CONF_PART, CONF_NAME = 0.7, 0.6, 0.5

_ERRORISH = re.compile(r"(?:^|_)(?:errors?|failures?|failed|fail|timeouts?)(?:_|$)")
_RATE = re.compile(r"(rate|irate|increase)\(\s*(.+?)\s*\[([^\]]+)\]\s*\)", re.DOTALL)
_AGG_HEAD = re.compile(r"(sum|avg|min|max)(?:\s+(?:by|without)\s*\([^()]*\))?\s*\(", re.IGNORECASE)
_AGG_TAIL = re.compile(r"\)\s*((?:by|without)\s*\([^()]*\))$", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Derived:
    lo: float
    hi: float
    bounds: str  # the catalog notation: "[0,1]", "[0,100]"
    basis: str  # one line: which rule, so the badge can say where the bounds came from
    unit: str  # "ratio" or "%": what the derived quantity is
    confidence: float

    @staticmethod
    def of(bounds: str, basis: str, confidence: float) -> Derived:
        lo, hi = natural_range(bounds)
        assert lo is not None and hi is not None
        return Derived(lo, hi, bounds, basis, "%" if hi == 100 else "ratio", confidence)


def _matching(e: str, i: int) -> int:
    """Index of the bracket closing the one at e[i], or -1."""
    depth, quote = 0, ""
    for j in range(i, len(e)):
        c = e[j]
        if quote:
            quote = "" if c == quote else quote
        elif c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _strip(e: str) -> str:
    e = e.strip()
    while e.startswith("(") and _matching(e, 0) == len(e) - 1:
        e = e[1:-1].strip()
    return e


def _split(e: str, ops: str) -> list[tuple[str, str]]:
    """Top-level binary split at the operator characters; [(operator before piece, piece)]."""
    out: list[tuple[str, str]] = []
    depth, quote, start, op = 0, "", 0, ""
    for j, c in enumerate(e):
        if quote:
            quote = "" if c == quote else quote
        elif c in "\"'`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c in ops and e[:j].strip() and e[:j].strip()[-1] not in "+-*/(":
            out.append((op, e[start:j]))
            start, op = j + 1, c
    out.append((op, e[start:]))
    return out


def _unwrap_agg(e: str) -> tuple[str, str, str] | None:
    """(function, clause, inner) for `sum by (x) (inner)` / `sum(inner) by (x)`, else None."""
    e = _strip(e)
    if m := _AGG_HEAD.match(e):
        close = _matching(e, m.end() - 1)
        if close == len(e) - 1:
            clause = re.sub(r"\s+", " ", e[len(m.group(1)) : m.end() - 1]).strip()
            return m.group(1).lower(), clause.lower(), e[m.end() : close]
        if t := _AGG_TAIL.match(e[close:]):
            return m.group(1).lower(), re.sub(r"\s+", " ", t.group(1)).lower(), e[m.end() : close]
    return None


def _matchers(m: str) -> frozenset[str]:
    body = m.strip()[1:-1] if m.strip() else ""
    parts, quote, cur = [], "", ""
    for c in body:
        if quote:
            quote = "" if c == quote else quote
        elif c in "\"'`":
            quote = c
        elif c == "," and not quote:
            parts.append(cur)
            cur = ""
            continue
        cur += c
    parts.append(cur)
    return frozenset(re.sub(r"\s+", "", p) for p in parts if p.strip())


def _family(name: str) -> str:
    """The metric's name without its error token, request token and `_total` suffix."""
    name = _ERRORISH.sub("_", name)
    name = re.sub(r"(?:^|_)requests?(?=_|$)", "_", name)
    name = re.sub(r"_total$", "", name)
    return re.sub(r"_+", "_", name).strip("_")


class _Rules:
    def __init__(self, bounds_of, nonneg, unit_of, bounded_by):
        self.bounds_of, self.nonneg, self.unit_of, self.bounded_by = (
            bounds_of, nonneg, unit_of, bounded_by,
        )  # fmt: skip

    def ratio01(self, expr: str) -> tuple[str, float] | None:
        """(basis, confidence) when `expr` is a [0,1] quantity."""
        e = _strip(expr)
        if not e:
            return None
        add = _split(e, "+-")
        if len(add) > 1:
            if (
                len(add) == 2
                and add[1][0] == "-"
                and _NUMBER.fullmatch(add[0][1].strip())
                and float(add[0][1]) == 1
            ):
                inner = self.ratio01(add[1][1])
                return (f"1 − ({inner[0]})", inner[1]) if inner else None
            return None
        mul = _split(e, "*/")
        if len(mul) == 2 and mul[1][0] == "/":
            return self.part_of(mul[0][1], mul[1][1])
        if len(mul) > 1:
            return None
        if agg := _unwrap_agg(e):
            if agg[0] in ("avg", "min", "max"):
                return self.ratio01(agg[2])
            return None
        if m := _RATE.fullmatch(e):
            parts = selector_parts(m.group(2))
            if m.group(1) != "increase" and parts and parts[0] in FRACTION_OF_TIME:
                return f"{m.group(1)} of {parts[0]} is a fraction of time per series", CONF_TIME
            return None
        if (p := selector_parts(e)) and self.bounds_of(p[0]) == "[0,1]":
            return f"{p[0]} is a catalog [0,1] ratio", CONF_PART
        return None

    def _side(self, e: str):
        agg = _unwrap_agg(e)
        head, inner = ((agg[0], agg[1]), agg[2]) if agg else (None, e)
        if m := _RATE.fullmatch(_strip(inner)):
            parts = selector_parts(m.group(2))
            return (head, (m.group(1), "".join(m.group(3).split()))), parts
        return (head, None), selector_parts(inner)

    def part_of(self, a: str, b: str) -> tuple[str, float] | None:
        (ha, pa), (hb, pb) = self._side(a), self._side(b)
        if pa is None or pb is None or ha != hb or (ha[0] and ha[0][0] != "sum"):
            return None
        (ma, xa), (mb, xb) = pa, pb
        if not (self.nonneg(ma) and self.nonneg(mb)):
            return None
        ua, ub = self.unit_of(ma), self.unit_of(mb)
        if ua != ub:  # different, or only one known: nothing says they measure the same thing
            return None
        if ma == mb:
            sa, sb = _matchers(xa), _matchers(xb)
            if sb < sa:
                return f"{ma} narrowed by extra label matchers is a part of {ma}", CONF_PART
            return None
        if ha[1] is None and self.bounded_by(ma, mb):
            return f"{ma} is bounded by {mb} (catalog bounded_by)", CONF_PART
        if _ERRORISH.search(ma) and not _ERRORISH.search(mb) and _family(ma) == _family(mb):
            return f"{ma} (errors) is a part of {mb} (same family, by name)", CONF_NAME
        return None


def derive_bounds(
    expr: str,
    *,
    bounds_of: Callable[[str], str | None],
    nonneg: Callable[[str], bool],
    unit_of: Callable[[str], str | None],
    bounded_by: Callable[[str, str], bool],
) -> Derived | None:
    """Bounds of a derived expression, or None when no rule applies."""
    rules = _Rules(bounds_of, nonneg, unit_of, bounded_by)
    e = _strip(expr)
    mul = _split(e, "*/") if e else []
    if len(mul) == 2 and mul[1][0] == "*":
        for k, x in ((0, 1), (1, 0)):
            if (
                _NUMBER.fullmatch(mul[k][1].strip())
                and float(mul[k][1]) == 100
                and (inner := rules.ratio01(mul[x][1]))
            ):
                return Derived.of("[0,100]", f"100 × ({inner[0]})", inner[1])
    if (r := rules.ratio01(e)) is None:
        return None
    return Derived.of("[0,1]", r[0], r[1])
