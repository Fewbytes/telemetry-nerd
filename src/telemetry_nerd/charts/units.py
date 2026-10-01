"""Infer a y-axis unit from Prometheus metric-name suffixes (telemetry-nerd-ch9).

Prometheus naming conventions make the unit a suffix of the metric name
(``_seconds``, ``_bytes``, ``_ratio``, ``_percent``), optionally wrapped by
the histogram/summary family (``_sum``, ``_bucket``, ``_count``, ``_total``).
This is a hint, not a catalog: the metric catalog (M3) will supply curated
units with real provenance later; until then this is honest best-effort.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from telemetry_nerd.catalog.rules import Facts, facts_from_name

_IDENT = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_TOKEN = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*|[()]")
_STRING = re.compile(r'"[^"]*"|`[^`]*`|\'')

# PromQL/MetricsQL keywords and function names: identifiers that are never
# metric names, so they must not participate in suffix inference.
_KEYWORDS = {
    "and",
    "or",
    "unless",
    "atan2",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "offset",
    "bool",
    "start",
    "end",
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "count_values",
    "stddev",
    "stdvar",
    "topk",
    "bottomk",
    "quantile",
    "group",
    "median",
    "rate",
    "irate",
    "resets",
    "changes",
    "increase",
    "delta",
    "idelta",
    "deriv",
    "predict_linear",
    "double_exponential_smoothing",
    "runs",
    "histogram_quantile",
    "histogram_count",
    "histogram_sum",
    "histogram_fraction",
    "histogram_avg",
    "histogram_stddev",
    "histogram_stdvar",
    "abs",
    "absent",
    "absent_over_time",
    "ceil",
    "floor",
    "round",
    "clamp",
    "clamp_max",
    "clamp_min",
    "scalar",
    "sgn",
    "sign",
    "sort",
    "sort_desc",
    "sort_by_label",
    "sort_by_label_desc",
    "sqrt",
    "exp",
    "ln",
    "log2",
    "log10",
    "deg",
    "rad",
    "pi",
    "time",
    "timestamp",
    "vector",
    "year",
    "month",
    "hour",
    "minute",
    "day_of_month",
    "day_of_week",
    "day_of_year",
    "days_in_month",
    "label_replace",
    "label_join",
    "info",
    "avg_over_time",
    "sum_over_time",
    "min_over_time",
    "max_over_time",
    "count_over_time",
    "quantile_over_time",
    "stddev_over_time",
    "stdvar_over_time",
    "present_over_time",
    "last_over_time",
    "mad_over_time",
}


def unit_from_metric_name(name: str) -> str | None:
    """Rule-only unit for a metric name (the catalog's T0 naming rules)."""
    return facts_from_name(name).unit


Lookup = Callable[[str], Facts]


def infer_unit(expr: str, lookup: Lookup = facts_from_name) -> str | None:
    return infer_unit_with_provenance(expr, lookup)[0]


def infer_unit_with_provenance(
    expr: str, lookup: Lookup = facts_from_name
) -> tuple[str | None, str | None]:
    """Infer the y-axis unit from every metric name in an expression.

    Returns the unit only when every metric name that carries a suffix agrees;
    conflicting or absent hints mean None (the UI stays honest).

    Enclosing functions transform the unit (b0v): rate/irate/deriv of a counter is
    per second (``count/s``, ``s/s``); histogram_quantile returns the metric's base
    unit whatever wraps its series; increase/delta keep the unit.
    """
    text = _STRING.sub(" ", expr)
    transforms: list[str | None] = []  # one entry per open paren: "rate", "quantile", None
    units: set[str] = set()
    provenance: set[str] = set()
    prev_ident = None
    for token in _TOKEN.findall(text):
        if token == "(":
            transforms.append(_transform_of_call(prev_ident))
            prev_ident = None
        elif token == ")":
            if transforms:
                transforms.pop()
            prev_ident = None
        else:
            prev_ident = token
            if token not in _KEYWORDS:
                unit, why = _unit_with_transforms(token, transforms, lookup)
                if unit is not None:
                    units.add(unit)
                    if why:
                        provenance.add(why)
    if len(units) != 1:
        return None, None
    return next(iter(units)), ", ".join(sorted(provenance)) or None


def _transform_of_call(prev_ident: str | None) -> str | None:
    """The unit transform a call opener applies to everything inside its parens."""
    if prev_ident == "histogram_quantile":
        return "quantile"
    if prev_ident in ("rate", "irate", "deriv"):
        return "rate"
    return None  # aggregators (sum/avg/…), scalar math, grouping: unit passes through


def _unit_with_transforms(
    name: str, transforms: list[str | None], lookup: Lookup
) -> tuple[str | None, str | None]:
    facts = lookup(name)
    unit = facts.unit
    if unit is None:
        return None, None
    if "quantile" in transforms:
        return unit, facts.unit_provenance  # a quantile is in the metric's base unit
    if "rate" in transforms and facts.type == "counter":
        # a per-second rate is only defined for counters (rate of a gauge stays put)
        return f"{unit}/s", facts.unit_provenance
    return unit, facts.unit_provenance


_COUNTER_SAFE = {
    "rate", "irate", "increase", "delta", "idelta", "deriv", "resets", "changes",
    "rollup_rate", "rollup_increase", "rollup_deriv", "histogram_quantile",
    "histogram_count", "histogram_sum", "count_over_time", "absent",
}  # fmt: skip


def raw_counters(expr: str, lookup: Lookup = facts_from_name) -> list[str]:
    """Counters used outside any rate-like call: a running total, not a signal."""
    calls: list[str | None] = []
    out: set[str] = set()
    prev: str | None = None
    for token in _TOKEN.findall(_STRING.sub(" ", expr)):
        if token == "(":
            calls.append(prev)
            prev = None
        elif token == ")":
            if calls:
                calls.pop()
            prev = None
        else:
            prev = token
            if (
                token not in _KEYWORDS
                and lookup(token).type == "counter"
                and not _COUNTER_SAFE.intersection(c for c in calls if c)
            ):
                out.add(token)
    return sorted(out)


def metric_names(expr: str) -> set[str]:
    """Identifiers in an expression that could be metric names (not keywords or functions).

    Over-approximates (label names are included); callers intersect with known metrics."""
    return {
        t for t in _TOKEN.findall(_STRING.sub(" ", expr)) if t not in "()" and t not in _KEYWORDS
    }
