"""Classify PromQL so percentiles are never aggregated (spec §1.2, principles 3-4).

A quantile expression must be outermost; we derive the number of observations
behind each value from the same histogram so a percentile always travels with n.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from telemetry_nerd.model.time import format_duration, parse_duration

RATE_INTERVAL = "$__rate_interval"
QUANTILE_HINT = (
    "never aggregate percentiles: aggregate the histogram first and take the quantile "
    "once, e.g. histogram_quantile(0.95, sum by (region) (rate(x[$__rate_interval]))); "
    "for summaries query one {quantile=...} selector as is"
)
_AGGREGATED = (
    "percentile aggregation refused: a quantile must be the outermost expression "
    "(optionally scaled by a number); wrapping it in sum/avg/max/over_time/arithmetic "
    "aggregates percentiles"
)
_UNSUPPORTED = (
    "percentile aggregation refused: quantile()/median()/median_over_time/quantiles_over_time/"
    "histogram_quantiles aggregate or multiply percentiles; use histogram_quantile(q, "
    "sum by (...) (rate(x[$__rate_interval]))) or quantile_over_time on a single series"
)
_QCALL = re.compile(r"\b(histogram_quantile|quantile_over_time)\s*\(", re.IGNORECASE)
# percentile functions/operators we cannot evaluate honestly per step: refuse them
_OTHER_QUANTILES = re.compile(
    r"\b(?:median_over_time|quantiles_over_time|histogram_quantiles"
    r"|(?:quantile|median)\s*(?:(?:by|without)\s*\([^()]*\)\s*)?\()",
    re.IGNORECASE,
)
_NUM = r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
_SCALE = re.compile(rf"^(?:\s*[*/]\s*{_NUM})+\s*$")
_LEFT_SCALE = re.compile(rf"^(?:\s*{_NUM}\s*\*)+\s*$")
_WINDOW = re.compile(r"\[([0-9]+(?:ms|s|m|h|d|w))(?::[^\]]*)?\]")
# The only shapes whose observation count we can derive: sum[by|without (..)]((rate|increase)(sel[w]))
# or a bare (rate|increase)(sel[w]). Anything else (arithmetic, avg/max, irate, subqueries) leaves n unknown.
_SIMPLE = re.compile(
    r"^\s*(?:sum\s*(?:(?:by|without)\s*\([^()]*\)\s*)?\(\s*(?P<f1>rate|increase)\s*\([^()]*\)\s*\)"
    r"(?:\s*(?:by|without)\s*\([^()]*\))?|(?P<f2>rate|increase)\s*\([^()]*\))\s*$",
    re.IGNORECASE,
)
_CLASSIC = re.compile(r"_bucket\b|\ble\b")
_SUMMARY_Q = re.compile(r"\bquantile\s*(?:=~?|!=|!~)\s*[\"'`]")
_SELECTOR = re.compile(r"^\s*(?:[a-zA-Z_:][a-zA-Z0-9_:]*\s*)?\{[^{}]*\}\s*$")


def min_samples(q: float) -> int:
    """Observations needed for a meaningful quantile: ~10 beyond it, n >= 10/(1-q)."""
    if not 0 < q < 1:
        raise ValueError(f"quantile must be in (0, 1), got {q}")
    return math.ceil(round(10 / (1 - q), 6))


def rate_interval_ms(step_ms: int, resolution_ms: int) -> int:
    """Grafana's $__rate_interval: at least 4 scrapes, and never shorter than a step."""
    return max(4 * resolution_ms, step_ms + resolution_ms)


def expand(expr: str, step_ms: int, resolution_ms: int) -> str:
    if RATE_INTERVAL not in expr:
        return expr
    return expr.replace(RATE_INTERVAL, format_duration(rate_interval_ms(step_ms, resolution_ms)))


@dataclass(frozen=True)
class QuantileExpr:
    q: float | None
    func: Literal["histogram_quantile", "quantile_over_time", "summary"]
    count_expr: str | None


@dataclass(frozen=True)
class ExprAnalysis:
    quantile: QuantileExpr | None = None
    problem: str | None = None


def _strip_comments(expr: str) -> str:
    """Drop `# ...` comments (outside quotes): a quote inside a comment must not open a string."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(expr):
        c = expr[i]
        if quote:
            out.append(c)
            if c == "\\" and quote != "`" and i + 1 < len(expr):
                out.append(expr[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
            out.append(c)
        elif c == "#":
            while i < len(expr) and expr[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _peel_parens(expr: str) -> str:
    """Remove redundant outer parentheses: `((x))` -> `x`."""
    while True:
        masked = _mask_strings(expr.strip())
        text = expr.strip()
        if masked.startswith("(") and masked and _close(masked, 0) == len(masked) - 1:
            expr = text[1:-1]
        else:
            return text


def _mask_strings(expr: str) -> str:
    """Blank out quoted text (keeping positions) so label values cannot fool the scanner."""
    out = list(expr)
    quote: str | None = None
    i = 0
    while i < len(expr):
        c = expr[i]
        if quote:
            if c == "\\" and quote != "`":
                out[i] = " "
                if i + 1 < len(expr):
                    out[i + 1] = " "
                i += 2
                continue
            if c == quote:
                quote = None
            else:
                out[i] = " "
        elif c in "\"'`":
            quote = c
        i += 1
    return "".join(out)


def _close(masked: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(masked)):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses in expression")


def _split_args(expr: str, masked: str, start: int, end: int) -> list[str]:
    args, depth, last = [], 0, start
    for i in range(start, end):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(expr[last:i].strip())
            last = i + 1
    args.append(expr[last:end].strip())
    return args


def _literal(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _count_expr(func: str, inner: str) -> str | None:
    if func == "quantile_over_time":
        return f"count_over_time({inner})"
    masked = _mask_strings(inner)
    m = _SIMPLE.match(masked)
    if m is None:
        return None
    total = (
        f"max without (le) ({inner})" if _CLASSIC.search(masked) else f"histogram_count({inner})"
    )
    if (m.group("f1") or m.group("f2")).lower() == "increase":
        return total
    windows = {w.group(1) for w in _WINDOW.finditer(masked)}
    if len(windows) != 1:
        return None
    return f"({total}) * {parse_duration(windows.pop()) / 1000:g}"


def analyze(expr: str) -> ExprAnalysis:
    expr = _peel_parens(_strip_comments(expr))
    masked = _mask_strings(expr)
    if _OTHER_QUANTILES.search(masked):
        return ExprAnalysis(problem=_UNSUPPORTED)
    calls = list(_QCALL.finditer(masked))
    if not calls:
        if _SUMMARY_Q.search(masked):
            if _SELECTOR.match(masked):
                return ExprAnalysis(QuantileExpr(None, "summary", None))
            return ExprAnalysis(problem=_AGGREGATED)
        return ExprAnalysis()
    first = calls[0]
    open_idx = first.end() - 1
    close_idx = _close(masked, open_idx)
    head, tail = masked[: first.start()], masked[close_idx + 1 :]
    if (
        len(calls) > 1
        or (head.strip() and not _LEFT_SCALE.match(head))
        or (tail.strip() and not _SCALE.match(tail))
    ):
        return ExprAnalysis(problem=_AGGREGATED)
    func = first.group(1).lower()
    args = _split_args(expr, masked, open_idx + 1, close_idx)
    if len(args) != 2:
        return ExprAnalysis(problem=f"{func} takes 2 arguments, got {len(args)}")
    return ExprAnalysis(QuantileExpr(_literal(args[0]), func, _count_expr(func, args[1])))  # type: ignore[arg-type]


_SUM = re.compile(r"^\s*sum\s*(?:by\s*\(([^()]*)\)\s*)?\(")
_WINDOWED_CALL = re.compile(r"^\s*(?:rate|increase)\s*\(")
_ANY_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*(\{[^{}]*\})?\s*$")


@dataclass(frozen=True)
class HistogramSource:
    selector: str
    by: tuple[str, ...]


def _unwrap(text: str, masked: str, pattern: re.Pattern) -> tuple[re.Match, str, str] | None:
    m = pattern.match(masked)
    if not m:
        return None
    open_idx = m.end() - 1
    close_idx = _close(masked, open_idx)
    if masked[close_idx + 1 :].strip():
        return None
    return m, text[open_idx + 1 : close_idx], masked[open_idx + 1 : close_idx]


def histogram_source(expr: str) -> HistogramSource | None:
    """The histogram behind histogram_quantile(q, sum by (L) (rate|increase(SEL[w]))), so a
    percentile panel can open the distribution it was computed from."""
    info = analyze(expr)
    if info.quantile is None or info.quantile.func != "histogram_quantile" or info.problem:
        return None
    masked = _mask_strings(expr)
    call = _QCALL.search(masked)
    open_idx = call.end() - 1
    args = _split_args(expr, masked, open_idx + 1, _close(masked, open_idx))
    inner = args[1]
    summed = _unwrap(inner, _mask_strings(inner), _SUM)
    if summed is None:
        return None
    m, body, mbody = summed
    by = tuple(x.strip() for x in (m.group(1) or "").split(",") if x.strip() and x.strip() != "le")
    windowed = _unwrap(body.strip(), mbody.strip(), _WINDOWED_CALL)
    if windowed is None:
        return None
    _, arg, marg = windowed
    bracket = marg.rfind("[")
    if bracket < 0 or not _ANY_SELECTOR.match(marg[:bracket]):
        return None
    return HistogramSource(arg[:bracket].strip(), by)


_HISTOGRAM_HINT = re.compile(r"_bucket\b|\bvmrange\b|\bby\s*\([^()]*\ble\b[^()]*\)", re.IGNORECASE)


def looks_like_histogram(expr: str) -> bool:
    """A non-quantile expression over histogram buckets: it will be drawn as lines, which
    hides the distribution (query_distribution draws a heatmap of counts)."""
    if analyze(expr).quantile is not None:
        return False
    return bool(_HISTOGRAM_HINT.search(_mask_strings(_strip_comments(expr))))
