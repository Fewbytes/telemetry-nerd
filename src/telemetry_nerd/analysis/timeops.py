"""Semantic preconditions for time ops (spec §5.1). Refusals carry a hint for Claude."""

from __future__ import annotations


def time_op_problem(
    op: str,
    representation: str,
    counters: list[str],
    percentiles: list[str] | None = None,
) -> str | None:
    """Why `op` cannot run on this dataset, or None. `percentiles` are catalogued metrics whose
    statistic claim says they are already-computed percentiles (a summary's quantile series, an
    exported p99 gauge): the same refusal as for a percentile series, from the catalog (2as.31)."""
    if percentiles:
        return (
            f"{op} refused on {', '.join(percentiles)}: the catalog says it is an already-computed "
            "percentile, and this would aggregate it over time or across series "
            "(hint: apply it to the request rate, to histogram_sum/histogram_count rates, or to "
            "threshold counts from query_distribution + fraction_over)"
        )
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
    if representation == "estimate":
        return (
            f"{op} needs a time series, this is a fit (parameters, no rows) "
            "(hint: apply it to the fit's prediction dataset, or cite the fit's parameters)"
        )
    if counters:
        return (
            f"{op} refused on raw counter {', '.join(counters)}: a running total has no periods "
            "(hint: use rate(x[$__rate_interval]))"
        )
    return None
