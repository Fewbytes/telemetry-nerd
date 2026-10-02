"""Bounds carried by derived expressions (bead f2z): a small, conservative rule library."""

import pytest

from telemetry_nerd.charts.derived_bounds import derive_bounds

BOUNDS = {"app_cache_hit_ratio": "[0,1]", "app_odd": None}
NONNEG = {"node_cpu_seconds_total", "app_errors_total", "app_requests_total", "mem_used_bytes",
          "mem_limit_bytes", "a_total", "b_seconds", "node_disk_read_errors_total",
          "node_network_receive_bytes_total", "api_errors_total",
          "api_request_duration_seconds_sum", "foo_errors_total", "foo_total",
          "foo_requests_total", "foo_bar_total"}  # fmt: skip
UNITS = {
    "node_cpu_seconds_total": "s",
    "node_network_receive_bytes_total": "B",
    "api_request_duration_seconds_sum": "s",
    "mem_used_bytes": "B",
    "mem_limit_bytes": "B",
    "a_total": "count",
    "b_seconds": "s",
}
BOUNDED_BY = {("mem_used_bytes", "mem_limit_bytes")}


def d(expr):
    return derive_bounds(
        expr,
        bounds_of=lambda m: BOUNDS.get(m),
        nonneg=lambda m: m in NONNEG,
        unit_of=lambda m: UNITS.get(m),
        bounded_by=lambda a, b: (a, b) in BOUNDED_BY,
    )


def test_one_minus_idle_rate_is_a_ratio():
    r = d('1 - rate(node_cpu_seconds_total{mode="idle"}[5m])')
    assert (r.lo, r.hi, r.bounds, r.unit) == (0.0, 1.0, "[0,1]", "ratio")
    assert "idle" in r.basis or "time" in r.basis


def test_bare_idle_rate_and_avg_over_cores():
    assert d('rate(node_cpu_seconds_total{mode="idle"}[5m])').bounds == "[0,1]"
    assert (
        d('1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))').bounds == "[0,1]"
    )


def test_sum_over_cores_is_not_bounded():
    assert d('1 - sum(rate(node_cpu_seconds_total{mode="idle"}[5m]))') is None


def test_increase_is_not_a_fraction_of_time():
    assert d('1 - increase(node_cpu_seconds_total{mode="idle"}[5m])') is None


def test_errors_over_total_same_family():
    r = d("sum(rate(app_errors_total[5m])) / sum(rate(app_requests_total[5m]))")
    assert (r.bounds, r.unit) == ("[0,1]", "ratio")


def test_same_metric_narrower_matchers_is_a_subset():
    r = d('sum(rate(a_total{code=~"5.."}[5m])) / sum(rate(a_total[5m]))')
    assert r is not None and r.bounds == "[0,1]"


def test_ratio_needs_matching_aggregation():
    assert (
        d("sum by (job) (rate(app_errors_total[5m])) / sum(rate(app_requests_total[5m]))") is None
    )


def test_used_over_limit_needs_the_bounded_by_relation():
    assert d("mem_used_bytes / mem_limit_bytes").bounds == "[0,1]"
    assert d("mem_limit_bytes / mem_used_bytes") is None


def test_unrelated_division_has_no_bounds():
    assert d("a_total / b_seconds") is None
    assert d("app_odd / app_requests_total") is None


def test_percent_of_ratio():
    r = d('100 * (1 - rate(node_cpu_seconds_total{mode="idle"}[5m]))')
    assert (r.lo, r.hi, r.bounds, r.unit) == (0.0, 100.0, "[0,100]", "%")
    assert d("app_cache_hit_ratio * 100").bounds == "[0,100]"


def test_one_minus_catalog_ratio():
    assert d("1 - app_cache_hit_ratio").bounds == "[0,1]"


@pytest.mark.parametrize(
    "expr",
    [
        "rate(a_total[5m])",
        "1 + app_cache_hit_ratio",
        "2 * app_cache_hit_ratio",
        "sum(app_cache_hit_ratio)",
        "1 -",
        "",
    ],
)
def test_everything_else_has_no_bounds(expr):
    assert d(expr) is None


@pytest.mark.parametrize(
    "expr",
    [
        "rate(node_disk_read_errors_total[5m]) / rate(node_cpu_seconds_total[5m])",
        "rate(node_disk_read_errors_total[5m]) / rate(node_network_receive_bytes_total[5m])",
        "rate(api_errors_total[5m]) / rate(api_request_duration_seconds_sum[5m])",
        "rate(foo_errors_total[5m]) / rate(foo_bar_total[5m])",
    ],
)
def test_error_name_rule_needs_the_same_family_and_unit(expr):
    assert d(expr) is None


def test_error_name_rule_accepts_equal_families():
    assert d("rate(foo_errors_total[5m]) / rate(foo_total[5m])").bounds == "[0,1]"
    assert d("rate(foo_errors_total[5m]) / rate(foo_requests_total[5m])").bounds == "[0,1]"


def test_part_of_needs_the_same_range_window():
    assert d('rate(a_total{code="500"}[1m]) / rate(a_total[1h])') is None
    assert d('rate(a_total{code="500"}[5m]) / rate(a_total[5m])').bounds == "[0,1]"
    assert d('rate(a_total{code="500"}[5m]) / irate(a_total[5m])') is None


def test_irate_basis_says_irate():
    assert "irate of" in d('1 - irate(node_cpu_seconds_total{mode="idle"}[5m])').basis
