"""Observed sample counts behind an expression's values (bead 1h9.11; spec 2026-10-02 §6).

A non-selector expression is evaluated by the adapter as a subquery `(expr)[step:res]`: a
series of instant evaluations, which lookback fills (profile `subquery_fills_gaps`), so
`count_over_time` of it counts evaluations, not samples. The honest count comes from the
expression's underlying selector, `count_over_time(sel[step])`, lifted through the expression's
aggregations with `sum` so its series are the expression's own series.

It is derived only where that is exact:

* exactly one vector selector, without `offset` / `@` / nested subqueries;
* label-preserving functions (`rate`, `increase`, `*_over_time`, `abs`, `clamp`, ...) whose
  other arguments are number literals;
* arithmetic with number literals (`x * 8`, `rate(x[1m]) / 1e3`);
* aggregations sum/avg/min/max/count/group/stddev/stdvar with by/without (observed = the
  members' samples), and `histogram_quantile(q, ...)` as an aggregation `without (le, vmrange)`.

Anything else (two selectors, comparisons/filters, set operators, vector matching, topk,
label_replace, unknown functions) returns None: the source cannot tell how many samples are
behind a value, and consumers must say so (`counts_are_observed`).
"""

from __future__ import annotations

import re

from telemetry_nerd.analysis.exprkind import _close, _mask_strings, _split_args, _strip_comments

_AGGREGATIONS = frozenset({"sum", "avg", "min", "max", "count", "group", "stddev", "stdvar"})
# one vector argument in, the same series (labels minus __name__) out
_PRESERVING = frozenset(
    {
        # range functions
        "rate",
        "irate",
        "increase",
        "delta",
        "idelta",
        "deriv",
        "predict_linear",
        "resets",
        "changes",
        "avg_over_time",
        "min_over_time",
        "max_over_time",
        "sum_over_time",
        "count_over_time",
        "last_over_time",
        "stddev_over_time",
        "stdvar_over_time",
        "present_over_time",
        "quantile_over_time",
        "mad_over_time",
        # instant, element-wise
        "abs",
        "ceil",
        "floor",
        "round",
        "exp",
        "ln",
        "log2",
        "log10",
        "sqrt",
        "sgn",
        "clamp",
        "clamp_min",
        "clamp_max",
        "deg",
        "rad",
        "histogram_count",
        "histogram_sum",
        "histogram_avg",
    }
)
_KEYWORDS = frozenset(
    {
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "bool",
        "offset",
        "and",
        "or",
        "unless",
        "atan2",
        "inf",
        "nan",
    }
)
_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_SELECTOR = re.compile(
    rf"^\s*(?P<sel>(?P<name>{_NAME})?\s*(?P<matchers>\{{[^{{}}]*\}})?)\s*"
    r"(?:\[\s*[0-9][0-9a-zA-Z]*\s*\])?\s*$"
)
_LITERAL = re.compile(
    r"^\s*[+-]?\s*(?:(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|0[xX][0-9a-fA-F]+|inf|nan)\s*$",
    re.IGNORECASE,
)
_CALL = re.compile(rf"^\s*(?P<f>{_NAME})\s*(?:(?P<kind>by|without)\s*\((?P<labels>[^()]*)\)\s*)?\(")
_TRAILING_CLAUSE = re.compile(r"^\s*(?P<kind>by|without)\s*\((?P<labels>[^()]*)\)\s*$")
# at the top level these change which series or samples exist: never derivable
_REJECT_OPS = re.compile(
    r"[=!<>@]|\b(?:and|or|unless|bool|on|ignoring|group_left|group_right|offset)\b"
)
_EXPONENT = re.compile(r"(?<![a-zA-Z0-9_:.])[0-9]*\.?[0-9]+[eE]$")
_ARITH = "+-*/%^"


def _peel(text: str, masked: str) -> tuple[str, str]:
    while True:
        lead = len(masked) - len(masked.lstrip())
        text, masked = text[lead:].rstrip(), masked[lead:].rstrip()
        if masked.startswith("(") and _close(masked, 0) == len(masked) - 1:
            text, masked = text[1:-1], masked[1:-1]
        else:
            return text, masked


def _operands(masked: str) -> list[tuple[int, int]] | None:
    """Spans of the top-level arithmetic operands; None if a comparison, set operator, vector
    matching or modifier appears at the top level."""
    spans: list[tuple[int, int]] = []
    depth, start, prev = 0, 0, ""
    top: list[str] = []
    for i, c in enumerate(masked):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if depth > 0 or c in ")]}":
            top.append(" ")
            if not c.isspace():
                prev = c
            continue
        top.append(c)
        if c in _ARITH:
            unary = prev == "" or prev in _ARITH
            exponent = c in "+-" and _EXPONENT.search(masked[start:i].rstrip()) is not None
            if not unary and not exponent:
                spans.append((start, i))
                start = i + 1
        if not c.isspace():
            prev = c
    spans.append((start, len(masked)))
    if _REJECT_OPS.search("".join(top)):
        return None
    return spans


def _lift(text: str, masked: str) -> tuple[str, list[str]] | None:
    """(selector, wrappers outermost first) or None if the count cannot be derived exactly."""
    text, masked = _peel(text, masked)
    spans = _operands(masked)
    if spans is None:
        return None
    if len(spans) > 1:
        vectors = [(a, b) for a, b in spans if not _LITERAL.match(masked[a:b])]
        if len(vectors) != 1:
            return None
        a, b = vectors[0]
        return _lift(text[a:b], masked[a:b])
    if (sign := re.match(r"^\s*[+-]", masked)) is not None:
        return _lift(text[sign.end() :], masked[sign.end() :])
    if (call := _CALL.match(masked)) is not None:
        return _lift_call(text, masked, call)
    sel = _SELECTOR.match(masked)
    if sel is None or not (sel.group("name") or sel.group("matchers")):
        return None
    if (sel.group("name") or "").lower() in _KEYWORDS:
        return None
    return text[sel.start("sel") : sel.end("sel")].strip(), []


def _lift_call(text: str, masked: str, call: re.Match) -> tuple[str, list[str]] | None:
    func = call.group("f").lower()
    open_idx = call.end() - 1
    close_idx = _close(masked, open_idx)
    args = _split_args(text, masked, open_idx + 1, close_idx)
    masked_args = _split_args(masked, masked, open_idx + 1, close_idx)
    rest = masked[close_idx + 1 :]
    kind, labels = call.group("kind"), call.group("labels")
    if func in _AGGREGATIONS:
        if rest.strip():
            trailing = _TRAILING_CLAUSE.match(rest)
            if trailing is None or kind:
                return None
            kind, labels = trailing.group("kind"), trailing.group("labels")
        if len(args) != 1:
            return None
        inner = _lift(args[0], masked_args[0])
        if inner is None:
            return None
        wrapper = "sum"
        if kind:
            names = ", ".join(x.strip() for x in labels.split(",") if x.strip())
            wrapper = f"sum {kind.lower()} ({names})"
        return inner[0], [wrapper, *inner[1]]
    if kind or rest.strip():
        return None
    vectors = [i for i, a in enumerate(masked_args) if not _LITERAL.match(a)]
    if len(vectors) != 1:
        return None
    if func == "histogram_quantile":
        if len(args) != 2 or vectors != [1]:
            return None
        inner = _lift(args[1], masked_args[1])
        return None if inner is None else (inner[0], ["sum without (le, vmrange)", *inner[1]])
    if func in _PRESERVING:
        return _lift(args[vectors[0]], masked_args[vectors[0]])
    return None


def observed_count_query(expr: str, window: str) -> str | None:
    """Query for the samples the source observed behind each value of `expr`, per series and
    bucket `window` (e.g. "1m"), or None if no exact count can be derived."""
    text = _strip_comments(expr)
    try:
        lifted = _lift(text, _mask_strings(text))
    except ValueError:  # unbalanced parentheses: let the source report the syntax error
        return None
    if lifted is None:
        return None
    selector, wrappers = lifted
    query = f"count_over_time({selector}[{window}])"
    for wrapper in reversed(wrappers):
        query = f"{wrapper} ({query})"
    return query


def counts_are_observed(expr: str) -> bool:
    """True if the adapter's `count` for `expr` is observed samples; False means the counts are
    subquery evaluations (source-filled) and coverage cannot be told from them."""
    return observed_count_query(expr, "1m") is not None
