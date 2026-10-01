"""Semantic preconditions for time ops (spec §5.1). Refusals carry a hint for Claude."""

from __future__ import annotations


def time_op_problem(op: str, representation: str, counters: list[str]) -> str | None:
    if representation == "quantile":
        return (
            f"{op} refused on a percentile series: it would aggregate percentiles over time "
            "(hint: apply it to the request rate, to histogram_sum/histogram_count rates, or to "
            "threshold counts from query_distribution + fraction_over)"
        )
    if representation == "distribution":
        return (
            f"{op} needs a time series, this is a distribution "
            "(hint: query a rate or a threshold count over time)"
        )
    if counters:
        return (
            f"{op} refused on raw counter {', '.join(counters)}: a running total has no periods "
            "(hint: use rate(x[$__rate_interval]))"
        )
    return None
