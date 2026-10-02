import pytest

from telemetry_nerd.sources.observed import counts_are_observed, observed_count_query

DERIVED = [
    ('syn_gauge{case="i60"} * 1', 'count_over_time(syn_gauge{case="i60"}[1m])'),
    ("rate(x[5m])", "count_over_time(x[1m])"),
    ("((rate(x[5m])))", "count_over_time(x[1m])"),
    ("-x / 1e-3", "count_over_time(x[1m])"),
    ("2 * abs(delta(x[10m]))", "count_over_time(x[1m])"),
    ("quantile_over_time(0.5, x[5m])", "count_over_time(x[1m])"),
    ("clamp(x, 0, 1)", "count_over_time(x[1m])"),
    ('x{quantile="0.9"}', 'count_over_time(x{quantile="0.9"}[1m])'),
    ('{__name__="x"}', 'count_over_time({__name__="x"}[1m])'),
    ("sum(rate(x[5m]))", "sum (count_over_time(x[1m]))"),
    (
        'sum by (job) (rate(http_requests_total{code=~"5.."}[5m])) * 60',
        'sum by (job) (count_over_time(http_requests_total{code=~"5.."}[1m]))',
    ),
    ("avg(rate(x[5m])) without (instance)", "sum without (instance) (count_over_time(x[1m]))"),
    (
        "histogram_quantile(0.9, sum by (le, job) (rate(x_bucket[5m])))",
        "sum without (le, vmrange) (sum by (le, job) (count_over_time(x_bucket[1m])))",
    ),
    (
        'histogram_quantile(0.9, rate(x_bucket{a="(b,c"}[5m]))',
        'sum without (le, vmrange) (count_over_time(x_bucket{a="(b,c"}[1m]))',
    ),
    (
        "sum by (r) (histogram_count(increase(x[1m])))",
        "sum by (r) (count_over_time(x[1m]))",
    ),
    (
        "max without (le) (increase(x_bucket[1m]))",
        "sum without (le) (count_over_time(x_bucket[1m]))",
    ),
]

CANNOT_TELL = [
    "rate(x[5m]) / rate(y[5m])",  # two selectors
    "x / on(job) group_left sum(x)",  # vector matching
    "x > 5",  # a filter: samples observed where no value is returned
    "x == bool 1",
    "x and y",
    "x unless y",
    "rate(x[5m] offset 1h)",  # the count would need the same offset
    "x @ 1700000000",
    "topk(5, x)",  # a subset of members
    "quantile(0.9, x)",
    'label_replace(x, "a", "$1", "b", "(.*)")',  # changes series identity
    "max_over_time(rate(x[1m])[5m:1m])",  # a nested subquery
    "absent(x)",
    "vector(1)",
    "1 + 2",
    "time()",
    "sum(rate(x[1m]",  # unbalanced: the source reports the syntax error
    "",
]


@pytest.mark.parametrize(("expr", "want"), DERIVED)
def test_count_comes_from_the_one_underlying_selector(expr, want):
    assert observed_count_query(expr, "1m") == want
    assert counts_are_observed(expr)


@pytest.mark.parametrize("expr", CANNOT_TELL)
def test_cannot_tell_returns_none(expr):
    assert observed_count_query(expr, "1m") is None
    assert not counts_are_observed(expr)


def test_window_is_the_bucket_and_comments_are_ignored():
    assert observed_count_query("rate(x[5m]) # per second\n", "30s") == "count_over_time(x[30s])"
