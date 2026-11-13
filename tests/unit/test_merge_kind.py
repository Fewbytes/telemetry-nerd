import pytest

from telemetry_nerd.analysis.mergekind import merge_kind

STEP = 15_000


@pytest.mark.parametrize(
    ("expr", "step", "resolution", "want"),
    [
        ("http_inflight", STEP, STEP, "sample_mean"),
        ('http_inflight{job="a/b"}', STEP, STEP, "sample_mean"),
        ("rate(x[1m])", STEP, STEP, "time_mean"),
        ("irate(x[1m])", STEP, STEP, "time_mean"),
        ("sum by (job) (rate(x[1m]))", STEP, STEP, "time_mean"),
        ('rate(x{path="/a/b"}[1m])', STEP, STEP, "time_mean"),
        ("increase(x[15s])", STEP, STEP, "sum"),
        ("sum(increase(x[15s]))", STEP, STEP, "sum"),
        ("changes(x[15s])", STEP, STEP, "sum"),
        # a window longer than the step, or a finer evaluation inside it, slides: never summed
        ("increase(x[5m])", STEP, STEP, "time_mean"),
        ("increase(x[1m])", 60_000, 15_000, "time_mean"),
        ("max_over_time(x[5m])", STEP, STEP, "max"),
        ("min_over_time(x[5m])", STEP, STEP, "min"),
        ("rate(a[1m]) / rate(b[1m])", STEP, STEP, "ratio"),
        ("histogram_quantile(0.99, sum by (le) (rate(x_bucket[5m])))", STEP, STEP, "quantile"),
        ("x > 5", STEP, STEP, "unknown"),
        ("sum(max_over_time(x[5m]))", STEP, STEP, "unknown"),
        ("stddev_over_time(x[5m])", STEP, STEP, "unknown"),
        ("max_over_time(rate(x[1m])[1h:])", STEP, STEP, "unknown"),
    ],
)
def test_merge_kind_follows_the_expression(expr, step, resolution, want):
    assert merge_kind(expr, step, resolution) == want
