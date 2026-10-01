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
_QCALL = re.compile(r"\b(histogram_quantile|quantile_over_time)\s*\(")
_SCALE = re.compile(r"^\s*[*/]\s*[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\s*$")
_WINDOW = re.compile(r"\[([0-9]+(?:ms|s|m|h|d|w))(?::[^\]]*)?\]")
_RATE = re.compile(r"\b(?:rate|irate)\s*\(")
_INCREASE = re.compile(r"\bincrease\s*\(")
_CLASSIC = re.compile(r"_bucket\b|\ble\b")
_SUMMARY_Q = re.compile(r"\bquantile\s*=~?\s*[\"'`]")
_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*\{[^{}]*\}\s*$")


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
    total = (
        f"max without (le) ({inner})" if _CLASSIC.search(masked) else f"histogram_count({inner})"
    )
    if _INCREASE.search(masked):
        return total
    if _RATE.search(masked):
        windows = {m.group(1) for m in _WINDOW.finditer(masked)}
        if len(windows) != 1:
            return None
        return f"({total}) * {parse_duration(windows.pop()) / 1000:g}"
    return None


def analyze(expr: str) -> ExprAnalysis:
    masked = _mask_strings(expr)
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
    if len(calls) > 1 or head.strip() or (tail.strip() and not _SCALE.match(tail)):
        return ExprAnalysis(problem=_AGGREGATED)
    args = _split_args(expr, masked, open_idx + 1, close_idx)
    if len(args) != 2:
        return ExprAnalysis(problem=f"{first.group(1)} takes 2 arguments, got {len(args)}")
    func = first.group(1)
    return ExprAnalysis(QuantileExpr(_literal(args[0]), func, _count_expr(func, args[1])))  # type: ignore[arg-type]
