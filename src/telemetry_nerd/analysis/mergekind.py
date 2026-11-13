"""How the buckets of a query combine across time (bead telemetry-nerd-7jme).

The count-weighted mean of bucket averages is the sample mean: right for a raw selector, wrong
for anything derived, whose sample counts belong to another series than the one plotted. Each
expression kind needs its own rule; only the cheap, certain shapes are classified, everything
else is `unknown` and is not merged locally.
"""

from __future__ import annotations

import re
from typing import Literal

from telemetry_nerd.analysis.exprkind import (
    _mask_strings,
    _peel_parens,
    _strip_comments,
    analyze,
    counter_rate_source,
    range_windows_ms,
    rate_functions,
    split_ratio,
)
from telemetry_nerd.sources.promql import is_selector

MergeKind = Literal["sample_mean", "time_mean", "sum", "max", "min", "ratio", "quantile", "unknown"]

#: kinds whose buckets cannot be combined locally: the dataset is shown at its own step
UNMERGEABLE: frozenset[MergeKind] = frozenset({"ratio", "quantile", "unknown"})

_LEAF = re.compile(r"^\s*(?P<f>[a-z_0-9]+)\s*\(\s*[^\[\]()]*\[[^\[\]:]+\]\s*\)\s*$", re.IGNORECASE)
_TIME_MEAN_FUNCS = frozenset({"rate", "irate", "deriv", "avg_over_time"})
# counts of events (or changes) in a window: their tiles add up
_TILE_FUNCS = frozenset(
    {"increase", "delta", "changes", "resets", "count_over_time", "sum_over_time"}
)


def merge_kind(expr: str, step_ms: int, resolution_ms: int) -> MergeKind:
    """The rule that combines the query buckets of `expr` (fetched at `step_ms`, evaluated every
    `resolution_ms`) into coarser ones.

    A window function of a selector counts events per window: its buckets add up only when the
    window is the step and each bucket holds one evaluation (a tile); a longer or overlapping
    window is a sliding level, which merges as a time mean."""
    if analyze(expr).quantile is not None:
        return "quantile"
    if is_selector(expr):
        return "sample_mean"
    leaf = _LEAF.match(_mask_strings(_peel_parens(_strip_comments(expr))))
    func = leaf.group("f").lower() if leaf else None
    if func is None and counter_rate_source(expr) is not None:
        func = "increase" if rate_functions(expr) == {"increase"} else "rate"
    if func in _TIME_MEAN_FUNCS:
        return "time_mean"
    if func in _TILE_FUNCS:
        tile = range_windows_ms(expr) == [step_ms] and resolution_ms == step_ms
        return "sum" if tile else "time_mean"
    if func == "max_over_time":
        return "max"
    if func == "min_over_time":
        return "min"
    return "ratio" if split_ratio(expr) is not None else "unknown"
