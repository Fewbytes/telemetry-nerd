"""TDD for the catalog-driven aggregation guard (telemetry-nerd-2as.20, spec §5 [H]/[SfE]):
flag avg/sum/max_over_time-style aggregation and multi-series merges over a metric whose
catalog `statistic` claim says it cannot be combined that way, even when nothing about the
PromQL syntax itself (no histogram_quantile, no `quantile=` selector) gives it away."""

from telemetry_nerd.catalog.mergeability import HARTMANN_CAVEAT
from telemetry_nerd.catalog.rules import Facts
from telemetry_nerd.charts.units import nonmergeable_uses


def lookup(statistics: dict[str, str]):
    return lambda name: Facts(None, None, None, statistics.get(name))


def test_plain_percentile_gauge_wrapped_in_avg_is_flagged():
    violations = nonmergeable_uses(
        "avg(app_latency_p99)", lookup({"app_latency_p99": "percentile"})
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.metric == "app_latency_p99"
    assert v.op == "avg"
    assert v.statistic == "percentile"
    assert v.reason
    assert v.caveat is None


def test_plain_percentile_gauge_wrapped_in_sum_by_is_flagged():
    violations = nonmergeable_uses(
        "sum by (region) (app_latency_p99)", lookup({"app_latency_p99": "percentile"})
    )
    assert len(violations) == 1
    assert violations[0].metric == "app_latency_p99"


def test_max_over_time_of_a_percentile_gauge_is_flagged():
    violations = nonmergeable_uses(
        "max_over_time(app_latency_p99[1h])", lookup({"app_latency_p99": "percentile"})
    )
    assert len(violations) == 1


def test_unwrapped_percentile_gauge_is_not_flagged():
    assert nonmergeable_uses("app_latency_p99", lookup({"app_latency_p99": "percentile"})) == []


def test_metric_with_no_statistic_claim_is_never_flagged():
    assert nonmergeable_uses("avg(some_gauge)", lookup({})) == []


def test_counter_rate_is_not_flagged_percentile_guard_is_separate_concern():
    # rate() isn't in the merge-function set at all: this guard only fires on aggregation ops
    assert nonmergeable_uses("rate(http_requests_total[5m])", lookup({})) == []


def test_mergeable_statistic_used_correctly_is_not_flagged():
    # count is mergeable via sum: summing per-series counts is exactly right
    assert nonmergeable_uses("sum(request_count)", lookup({"request_count": "count"})) == []


def test_mergeable_statistic_averaged_is_flagged():
    violations = nonmergeable_uses(
        "avg(request_max_latency)", lookup({"request_max_latency": "max"})
    )
    assert len(violations) == 1
    assert violations[0].statistic == "max"


def test_override_attaches_hartmann_caveat_but_still_reports_the_violation():
    violations = nonmergeable_uses(
        "avg(app_latency_p99)", lookup({"app_latency_p99": "percentile"}), override=True
    )
    assert len(violations) == 1
    assert violations[0].caveat == HARTMANN_CAVEAT


def test_multiple_violations_across_an_expression():
    violations = nonmergeable_uses(
        "avg(app_latency_p99) / avg(other_p95)",
        lookup({"app_latency_p99": "percentile", "other_p95": "percentile"}),
    )
    assert {v.metric for v in violations} == {"app_latency_p99", "other_p95"}
