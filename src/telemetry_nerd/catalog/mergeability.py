"""Mergeability table for statistics (spec §5 [SfE]/[H]): which ones may be aggregated across
time or across series, and which must never be.

From `docs/telemetry-graphing-guide.md` §5:

| statistic | mergeable | robust | aggregate across time/series? |
|---|---|---|---|
| count, count_below(X) | yes | yes | sum |
| ratio_below(X) | only with total count | yes | ratio of sums, never mean of ratios |
| mean | only with count | no | count-weighted |
| min / max | yes | no | min / max |
| median, percentile, truncated mean, MAD, IQR | no | yes | forbidden |

Prometheus summary `quantile=` series and exported p99-style gauges are the `percentile` case:
catalog T0 rules (`catalog.rules`) mark them so, and this module is the validator that rejects
aggregating them (and generalizes the rejection to every non-mergeable statistic, not only
percentiles).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mergeability = Literal["mergeable", "needs_counts", "non_mergeable"]

#: mergeable only via this exact combining operator; any other op loses information
#: (e.g. avg of maxima is not the maximum, avg of sums double counts nothing but is still
#: not what "sum" means structurally)
NATIVE_OP: dict[str, str] = {
    "count": "sum",
    "count_below": "sum",
    "sum": "sum",
    "min": "min",
    "max": "max",
}

#: mergeable only when combined with the per-series/per-bucket observation count; the named
#: op is the one honest way to do it (spec: "count-weighted" mean, "ratio of sums" for ratios)
WEIGHTED_OP: dict[str, str] = {
    "mean": "count_weighted_mean",
    "ratio": "ratio_of_sums",
}

#: never mergeable: recompute from the merged histogram/raw data instead of combining the
#: already-computed statistic
NON_MERGEABLE = frozenset({"median", "percentile", "truncated_mean", "mad", "iqr"})

#: the canonical caveat text (spec §5 [H], Hartmann SREcon19 EMEA): averaging already-computed
#: percentiles is badly wrong in practice. Exact numbers are a regression fixture — don't
#: round them differently in two places.
HARTMANN_CAVEAT = (
    "Averaging pre-computed percentiles is badly wrong in practice: 24 hourly p90 values "
    "averaged to 60.3 ms, while the true p90 of the 811k merged requests was 35.8 ms — a "
    "68.5% error. Cause: low-traffic hours (25-600 req, wide spread) weigh the same as peak "
    "hours (35-98k req). Recompute the statistic from the merged histogram or raw data instead "
    "of averaging/summing/merging the already-computed values."
)


#: PromQL/MetricsQL `*_over_time` spellings mean the same combining operator across time as
#: their bare counterpart means across series (`sum_over_time` : `sum` :: merges by adding).
#: Callers may pass either spelling; `check_aggregation` normalizes through this table.
OP_ALIASES: dict[str, str] = {
    "sum_over_time": "sum",
    "min_over_time": "min",
    "max_over_time": "max",
    "count_over_time": "sum",
    "avg_over_time": "avg",
    "stddev_over_time": "stddev",
    "stdvar_over_time": "stdvar",
    "quantile_over_time": "quantile",
    "median_over_time": "median",
    "mad_over_time": "mad",
}


def normalize_op(op: str) -> str:
    """The canonical op name for an aggregation spelling (collapses *_over_time variants)."""
    return OP_ALIASES.get(op, op)


def mergeability(statistic: str) -> Mergeability:
    """Classify a statistic kind per the table above."""
    if statistic in NON_MERGEABLE:
        return "non_mergeable"
    if statistic in WEIGHTED_OP:
        return "needs_counts"
    if statistic in NATIVE_OP:
        return "mergeable"
    raise ValueError(
        f"unknown statistic {statistic!r}; expected one of "
        f"{sorted(NON_MERGEABLE | set(WEIGHTED_OP) | set(NATIVE_OP))}"
    )


@dataclass(frozen=True)
class AggregationCheck:
    forbidden: bool
    #: why the aggregation is wrong; None when forbidden is False
    reason: str | None = None
    #: set only when forbidden and the caller passed override=True: the Hartmann caveat to
    #: render alongside the (still dubious) result the user asked for anyway
    caveat: str | None = None


def check_aggregation(statistic: str, op: str, *, override: bool = False) -> AggregationCheck:
    """Is `op` (e.g. "avg", "sum", "max_over_time", a cross-series merge, ...) a meaningful way
    to combine this statistic across time or series?

    Mergeable statistics (count, min, max, sum) are only right with their one native op;
    needs-counts statistics (mean, ratio) are only right when explicitly count-weighted;
    non-mergeable statistics (median, percentile, truncated_mean, mad, iqr) are never right —
    this is the general form of "never aggregate pre-computed quantiles" (spec §5 [H]).

    When forbidden and `override=True`, the check still reports forbidden=True (the caller
    decides whether to proceed) but attaches the Hartmann caveat text to show the user.
    """
    raw_op, op = op, normalize_op(op)
    kind = mergeability(statistic)
    if kind == "non_mergeable":
        reason = (
            f"{statistic} is not mergeable: {raw_op!r} would aggregate an already-computed "
            f"statistic across time or series, which is not statistically meaningful — "
            "recompute it from the merged histogram/raw data instead"
        )
        allowed = False
    elif kind == "needs_counts":
        wanted = WEIGHTED_OP[statistic]
        allowed = op == wanted
        reason = None
        if not allowed:
            reason = (
                f"{statistic} is only mergeable with access to the per-series/per-bucket "
                f"counts ({wanted}); {raw_op!r} is a naive aggregate and silently mis-weights "
                "low-traffic and high-traffic periods/series the same"
            )
    else:  # mergeable
        wanted = NATIVE_OP[statistic]
        allowed = op == wanted
        reason = None
        if not allowed:
            reason = (
                f"{statistic} is mergeable only via {wanted!r} across time/series; "
                f"{raw_op!r} is not the right aggregator for it"
            )
    if allowed:
        return AggregationCheck(forbidden=False)
    return AggregationCheck(
        forbidden=True, reason=reason, caveat=HARTMANN_CAVEAT if override else None
    )


def is_precomputed_quantile(statistic: str | None) -> bool:
    """True for the specific case the bead names: Prometheus summary `quantile=` series and
    exported p99-style gauges (median counts too: it is percentile 0.5)."""
    return statistic in ("percentile", "median")


def non_aggregatable(statistic: str | None) -> bool:
    """True when this statistic must never be aggregated across time or series at all."""
    return statistic is not None and statistic in NON_MERGEABLE
