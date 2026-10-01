import pytest

from telemetry_nerd.analysis.exprkind import (
    HistogramSource,
    analyze,
    expand,
    histogram_source,
    min_samples,
    rate_interval_ms,
)

NATIVE = 'sum by (cloud_region) (rate(lat{svc="c"}[5m]))'
CLASSIC = 'sum by (le, region) (rate(lat_seconds_bucket{svc="c"}[2m]))'


@pytest.mark.parametrize(
    ("q", "n"), [(0.5, 20), (0.9, 100), (0.95, 200), (0.99, 1000), (0.999, 10000)]
)
def test_min_samples(q, n):
    assert min_samples(q) == n


@pytest.mark.parametrize("q", [0, 1, -0.1, 1.5])
def test_min_samples_rejects_out_of_range(q):
    with pytest.raises(ValueError):
        min_samples(q)


def test_rate_interval_and_expand():
    assert rate_interval_ms(30_000, 20_000) == 80_000  # 4 x scrape wins
    assert rate_interval_ms(300_000, 15_000) == 315_000  # step + scrape wins
    assert expand("rate(x[$__rate_interval])", 30_000, 20_000) == "rate(x[80s])"
    assert expand("rate(x[1m])", 30_000, 20_000) == "rate(x[1m])"


def test_plain_expression_is_not_quantile():
    a = analyze("sum by (svc) (rate(http_requests_total[5m]))")
    assert a.quantile is None and a.problem is None


def test_native_histogram_quantile_with_rate():
    a = analyze(f"histogram_quantile(0.95, {NATIVE})")
    assert a.problem is None
    assert a.quantile.func == "histogram_quantile"
    assert a.quantile.q == 0.95
    assert a.quantile.count_expr == f"(histogram_count({NATIVE})) * 300"


def test_classic_histogram_quantile_with_rate():
    a = analyze(f"histogram_quantile(0.99, {CLASSIC})")
    assert a.quantile.q == 0.99
    assert a.quantile.count_expr == f"(max without (le) ({CLASSIC})) * 120"


def test_increase_needs_no_window_multiplier():
    inner = "sum by (le) (increase(lat_bucket[10m]))"
    assert analyze(f"histogram_quantile(0.5, {inner})").quantile.count_expr == (
        f"max without (le) ({inner})"
    )


def test_mixed_windows_leave_n_unknown():
    inner = "sum by (le) (rate(a_bucket[1m])) + sum by (le) (rate(b_bucket[5m]))"
    a = analyze(f"histogram_quantile(0.9, {inner})")
    assert a.problem is None
    assert a.quantile.count_expr is None


def test_quantile_over_time_counts_samples():
    a = analyze('quantile_over_time(0.9, queue_depth{q="x"}[10m])')
    assert a.quantile.func == "quantile_over_time"
    assert a.quantile.count_expr == 'count_over_time(queue_depth{q="x"}[10m])'


def test_scaling_by_a_literal_is_allowed():
    a = analyze(f"histogram_quantile(0.95, {NATIVE}) * 1000")
    assert a.problem is None and a.quantile.q == 0.95


@pytest.mark.parametrize(
    "expr",
    [
        f"sum(histogram_quantile(0.95, {NATIVE}))",
        f"avg by (x) (histogram_quantile(0.95, {NATIVE}))",
        f"max_over_time(histogram_quantile(0.95, {NATIVE})[1h:])",
        f"histogram_quantile(0.95, {NATIVE}) + histogram_quantile(0.5, {NATIVE})",
        f"histogram_quantile(0.95, {NATIVE}) / on() group_left sum(up)",
        'avg(rpc_latency{quantile="0.99"})',
    ],
)
def test_aggregated_percentiles_are_refused(expr):
    a = analyze(expr)
    assert a.quantile is None
    assert a.problem is not None and "percentile" in a.problem


def test_summary_quantile_selector_allowed_with_unknown_n():
    a = analyze('rpc_latency{quantile="0.99", job="api"}')
    assert a.problem is None
    assert a.quantile.func == "summary"
    assert a.quantile.count_expr is None


def test_quoted_text_does_not_fool_the_scanner():
    a = analyze('sum(rate(x{path="histogram_quantile(0.9, y)"}[5m]))')
    assert a.quantile is None and a.problem is None


def test_non_literal_quantile_is_kept_without_q():
    a = analyze(f"histogram_quantile(scalar(foo), {NATIVE})")
    assert a.quantile.q is None


HQ = "histogram_quantile(0.95, sum by (le) (rate(x_bucket[5m])))"


@pytest.mark.parametrize(
    "expr",
    [
        f"# p95, don't aggregate\navg({HQ})",  # an apostrophe in a comment must not hide the call
        f"avg({HQ.upper()})",  # MetricsQL function names are case-insensitive
        "quantile(0.95, rate(x[5m]))",
        "quantile by (job) (0.95, rate(x[5m]))",
        "median(rate(x[5m]))",
        "median_over_time(x[10m])",
        'quantiles_over_time("phi", 0.5, 0.9, x[10m])',
        'sum(x{quantile!="0.99"})',
        'max_over_time(x{quantile!~"0.5"}[1h])',
    ],
)
def test_review_loopholes_are_refused(expr):
    a = analyze(expr)
    assert a.quantile is None and a.problem is not None


@pytest.mark.parametrize(
    "expr",
    [
        f"1000 * {HQ}",
        f"{HQ} # note",
        f"({HQ})",
        f"{HQ} * 1000 / 2",
        "sum(rate(x[5m])) # histogram_quantile(",
    ],
)
def test_review_false_positives_are_accepted(expr):
    assert analyze(expr).problem is None


@pytest.mark.parametrize(
    "inner",
    [
        "sum by (le) (irate(x_bucket[5m]))",  # irate uses ~1 scrape, not the window
        "sum by (le) (rate(x_bucket[5m])) * 60",  # scaling would inflate n
        "avg by (le) (rate(x_bucket[5m]))",
    ],
)
def test_count_is_not_derived_when_it_would_be_wrong(inner):
    assert analyze(f"histogram_quantile(0.9, {inner})").quantile.count_expr is None


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ('histogram_quantile(0.95, sum by (cloud_region) (rate(lat{svc="c"}[5m])))',
         HistogramSource('lat{svc="c"}', ("cloud_region",))),
        ("histogram_quantile(0.99, sum by (le, region) (increase(x_bucket[2m])))",
         HistogramSource("x_bucket", ("region",))),
        ("histogram_quantile(0.5, sum(rate(x_bucket[1m])))", HistogramSource("x_bucket", ())),
        ("histogram_quantile(0.5, rate(x_bucket[1m]))", None),  # no sum: per-series, cannot express as by
        ("quantile_over_time(0.9, q[5m])", None),
        ("sum(rate(x[5m]))", None),
        ('histogram_quantile(0.9, sum by (le) (rate(x_bucket{p="a[1m]"}[1m])))',
         HistogramSource('x_bucket{p="a[1m]"}', ())),
    ],
)  # fmt: skip
def test_histogram_source(expr, want):
    assert histogram_source(expr) == want


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ('sum(rate(lat_seconds_bucket{job="a"}[5m]))', True),
        ("sum by (le) (rate(x[5m]))", True),
        ("sum by (vmrange) (histogram_over_time(x[5m]))", True),
        ("histogram_quantile(0.95, sum by (le) (rate(x_bucket[5m])))", False),
        ("sum(rate(http_requests_total[5m]))", False),
        ('up{path="/metrics_bucket"}', False),
    ],
)
def test_looks_like_histogram(expr, want):
    from telemetry_nerd.analysis.exprkind import looks_like_histogram

    assert looks_like_histogram(expr) is want
