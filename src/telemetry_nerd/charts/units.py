"""Infer a y-axis unit from Prometheus metric-name suffixes (telemetry-nerd-ch9).

Prometheus naming conventions make the unit a suffix of the metric name
(``_seconds``, ``_bytes``, ``_ratio``, ``_percent``), optionally wrapped by
the histogram/summary family (``_sum``, ``_bucket``, ``_count``, ``_total``).
This is a hint, not a catalog: the metric catalog (M3) will supply curated
units with real provenance later; until then this is honest best-effort.
"""

from __future__ import annotations

import re

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

# Base-unit suffixes (no suffix is a prefix of another; order is for reading).
_SUFFIX_UNITS = (
    ("_seconds", "s"),
    ("_bytes", "B"),
    ("_ratio", "ratio"),
    ("_percent", "%"),
)

# Histogram/summary family suffixes that wrap a base-unit name
# (e.g. tn_demo_latency_seconds_sum / _bucket carry the base unit).
_AGG_SUFFIXES = ("_sum", "_bucket")


def unit_from_metric_name(name: str) -> str | None:
    """Map one metric name to a unit via its suffix, or None if unknown."""
    had_total = name.endswith("_total")
    base = name[: -len("_total")] if had_total else name
    if base.endswith("_count"):
        # A count series counts events regardless of the base unit
        # (x_seconds_count is a number of observations, not seconds).
        return "count"
    for agg in _AGG_SUFFIXES:
        if base.endswith(agg):
            base = base[: -len(agg)]
            break
    for suffix, unit in _SUFFIX_UNITS:
        if base.endswith(suffix):
            return unit
    if had_total:
        return "count"  # bare counter suffix: a number of events
    return None


def infer_unit(expr: str) -> str | None:
    """Infer the y-axis unit from every metric name in an expression.

    Returns the unit only when every metric name that carries a suffix agrees;
    conflicting or absent hints mean None (the UI stays honest).

    Enclosing functions transform the unit (b0v): rate/irate/deriv of a counter is
    per second (``count/s``, ``s/s``); histogram_quantile returns the metric's base
    unit whatever wraps its series; increase/delta keep the unit.
    """
    text = _STRING.sub(" ", expr)
    transforms: list[str | None] = []  # one entry per open paren: "rate", "quantile", None
    units = set()
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
                unit = _unit_with_transforms(token, transforms)
                if unit is not None:
                    units.add(unit)
    return next(iter(units)) if len(units) == 1 else None


def _transform_of_call(prev_ident: str | None) -> str | None:
    """The unit transform a call opener applies to everything inside its parens."""
    if prev_ident == "histogram_quantile":
        return "quantile"
    if prev_ident in ("rate", "irate", "deriv"):
        return "rate"
    return None  # aggregators (sum/avg/…), scalar math, grouping: unit passes through


def _unit_with_transforms(name: str, transforms: list[str | None]) -> str | None:
    unit = unit_from_metric_name(name)
    if unit is None:
        return None
    if "quantile" in transforms:
        return unit  # a quantile of the distribution is in the metric's base unit
    if "rate" in transforms and name.endswith("_total"):
        # a per-second rate is only defined for counters (rate of a gauge stays put)
        return f"{unit}/s"
    return unit
