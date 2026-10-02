import pytest

from telemetry_nerd.sources.observed import (
    MAX_COUNT_QUERY_LEN,
    MAX_FOLD_OPERANDS,
    counts_are_observed,
    observed_count_query,
)

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
    "a / on(job) b",
    "a / ignoring(x) b",
    "a / b offset 1m",
    "topk(5, a / b)",
    "a / b and c",
    "sum(a / b)",
    "abs(a / b)",
    "clamp_max(a / b, 1)",
    "-(a / b)",
    'label_replace(a / b, "x", "$1", "y", "(.*)")',
    "up == 0",
    "a / (b > 1)",
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


def _min2(x, y):
    return f"(({x}) <= ({y})) or (({y}) and ({x}))"


def test_ratio_of_aggregates_folds_with_min():
    got = observed_count_query("sum(rate(a[5m])) / sum(rate(b[5m]))", "1m")
    assert got == _min2("sum (count_over_time(a[1m]))", "sum (count_over_time(b[1m]))")


@pytest.mark.parametrize(
    "expr",
    [
        "rate(err[5m]) / rate(total[5m])",
        "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
        "100 * (1 - a / b)",
        "(a / b) * c",
    ],
)
def test_binary_arithmetic_between_observables_is_observed(expr):
    assert observed_count_query(expr, "1m") is not None
    assert counts_are_observed(expr)


def test_three_operands_fold_pairwise():
    inner = _min2("count_over_time(a[1m])", "count_over_time(b[1m])")
    assert observed_count_query("(a / b) * c", "1m") == _min2(inner, "count_over_time(c[1m])")


def test_repeated_operands_are_deduplicated():
    q = observed_count_query("(MemTotal - MemAvailable) / MemTotal", "1m")
    assert q == _min2("count_over_time(MemTotal[1m])", "count_over_time(MemAvailable[1m])")
    assert q.count("count_over_time") == 4


def test_too_many_distinct_operands_cannot_be_told():
    names = "abcdefg"
    ok = " + ".join(names[:MAX_FOLD_OPERANDS])
    assert observed_count_query(ok, "1m") is not None
    assert observed_count_query(" + ".join(names[: MAX_FOLD_OPERANDS + 1]), "1m") is None


def test_oversized_fold_cannot_be_told():
    long = [f"{n}_{'x' * (MAX_COUNT_QUERY_LEN // 8)}" for n in "abcd"]
    assert observed_count_query(" + ".join(long), "1m") is None
    assert observed_count_query(" + ".join(long[:2]), "1m") is not None
