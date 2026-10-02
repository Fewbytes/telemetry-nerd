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
  members' samples), and `histogram_quantile(q, ...)` as an aggregation `without (le, vmrange)`;
* top-level arithmetic between two or more such operands (`a / b`): observed = the smaller
  operand count per series and bucket (see `observed_count_query`), capped in size.

* those wrappers (functions, unary minus, aggregations) around such an arithmetic expression
  (`avg(a / b)`, `abs(a / b)`, `sum by (job) (rate(a[5m]) / rate(b[5m]))`): the fold is passed
  through label-preserving functions and summed by aggregations.

Anything else (comparisons/filters, set operators, vector matching, offset/@, topk,
label_replace, more than `MAX_FOLD_OPERANDS` distinct operands or a query longer than
`MAX_COUNT_QUERY_LEN`, unknown functions) returns None: the source cannot tell how many samples
are behind a value, and consumers must say so (`counts_are_observed`).
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
# the min fold repeats its operands (2^(n-1) copies of the first): cap it, since a count query
# the backend rejects would fail the whole fetch where "cannot tell" merely degrades to UNKNOWN
MAX_FOLD_OPERANDS = 4
MAX_COUNT_QUERY_LEN = 4096
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


# a leaf is a count query and the number of distinct selector operands folded inside it
_Leaf = tuple[str, int]


def _fold(leaves: list[_Leaf]) -> _Leaf | None:
    """Fold leaf count queries with the per-series min (min(x, x) = x, so repeats are dropped);
    None if the distinct operands or the query exceed the caps."""
    distinct = list(dict.fromkeys(leaves))
    if sum(n for _, n in distinct) > MAX_FOLD_OPERANDS:
        return None
    query = distinct[0][0]
    for part, _ in distinct[1:]:
        query = _min2(query, part)
    return (query, sum(n for _, n in distinct)) if len(query) <= MAX_COUNT_QUERY_LEN else None


def _wrap(wrapper: str, inner: list[str] | None) -> list[_Leaf] | None:
    """The fold of `inner` leaves under an aggregation `wrapper` (a sum of the members' samples)."""
    folded = None if inner is None else _fold(inner)
    return None if folded is None else [(f"{wrapper} ({folded[0]})", folded[1])]


def _leaves(text: str, masked: str, window: str) -> list[_Leaf] | None:
    """Count queries of the leaf operands of an expression; None if any cannot be derived.
    Top-level arithmetic flattens (min is associative); label-preserving functions and unary
    signs pass their operand through; an aggregation folds its operand and wraps it in `sum`."""
    text, masked = _peel(text, masked)
    spans = _operands(masked)
    if spans is None:
        return None
    if len(spans) > 1:
        vectors = [(a, b) for a, b in spans if not _LITERAL.match(masked[a:b])]
        if not vectors:
            return None
        found: list[_Leaf] = []
        for a, b in vectors:
            part = _leaves(text[a:b], masked[a:b], window)
            if part is None:
                return None
            found += part
        return found
    if (sign := re.match(r"^\s*[+-]", masked)) is not None:
        return _leaves(text[sign.end() :], masked[sign.end() :], window)
    if (call := _CALL.match(masked)) is not None:
        return _call_leaves(text, masked, call, window)
    sel = _SELECTOR.match(masked)
    if sel is None or not (sel.group("name") or sel.group("matchers")):
        return None
    if (sel.group("name") or "").lower() in _KEYWORDS:
        return None
    selector = text[sel.start("sel") : sel.end("sel")].strip()
    return [(f"count_over_time({selector}[{window}])", 1)]


def _call_leaves(text: str, masked: str, call: re.Match, window: str) -> list[_Leaf] | None:
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
        wrapper = "sum"
        if kind:
            names = ", ".join(x.strip() for x in labels.split(",") if x.strip())
            wrapper = f"sum {kind.lower()} ({names})"
        return _wrap(wrapper, _leaves(args[0], masked_args[0], window))
    if kind or rest.strip():
        return None
    vectors = [i for i, a in enumerate(masked_args) if not _LITERAL.match(a)]
    if len(vectors) != 1:
        return None
    inner = _leaves(args[vectors[0]], masked_args[vectors[0]], window)
    if func == "histogram_quantile":
        if len(args) != 2 or vectors != [1]:
            return None
        return _wrap("sum without (le, vmrange)", inner)
    return inner if func in _PRESERVING else None


def _count_query(text: str, masked: str, window: str) -> str | None:
    leaves = _leaves(text, masked, window)
    folded = None if leaves is None else _fold(leaves)
    return None if folded is None else folded[0]


def _min2(x: str, y: str) -> str:
    """Per-series minimum of two count vectors, keeping only series present on both sides:
    `x <= y` keeps x where it is the smaller (or equal), `y and x` keeps y's series that also
    exist in x, and `or` adds those where y is the smaller."""
    return f"(({x}) <= ({y})) or (({y}) and ({x}))"


def observed_count_query(expr: str, window: str) -> str | None:
    """Query for the samples the source observed behind each value of `expr`, per series and
    bucket `window` (e.g. "1m"), or None if no exact count can be derived.

    For arithmetic between two or more observable vector operands (`a / b`, `(a / b) * c`) the
    operands' count queries are folded pairwise with `_min2`: observed per bucket is the smaller
    of the operands' observed sample counts (a ratio is only as observed as its sparser side).
    Series present on only one side are absent, as they are from the expression's own result."""
    text = _strip_comments(expr)
    try:
        return _count_query(text, _mask_strings(text), window)
    except ValueError:  # unbalanced parentheses: let the source report the syntax error
        return None


def counts_are_observed(expr: str) -> bool:
    """True if the adapter's `count` for `expr` is observed samples; False means the counts are
    subquery evaluations (source-filled) and coverage cannot be told from them."""
    return observed_count_query(expr, "1m") is not None
