import math

import pytest

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.analysis.histogram import (
    cumulative_to_buckets,
    from_matrix,
    histogram_expr,
    native_schema,
    parse_vmrange,
)
from telemetry_nerd.model.distribution import DIST_N_MIN, BucketScheme
from telemetry_nerd.model.series import series_id

INF = math.inf


def test_dist_n_min_is_the_median_rule():
    assert DIST_N_MIN == min_samples(0.5) == 20


def test_histogram_expr_groups_by_both_bucket_labels():
    assert histogram_expr('lat_bucket{job="a"}', ["region"], 60_000) == (
        'sum by (le, vmrange, region) (increase(lat_bucket{job="a"}[1m]))'
    )


@pytest.mark.parametrize("label", ["le", "vmrange", "a-b", ""])
def test_histogram_expr_rejects_bad_labels(label):
    with pytest.raises(ValueError):
        histogram_expr("x_bucket", [label], 60_000)


def test_cumulative_to_per_bucket_with_inf_bucket():
    buckets, n, problems = cumulative_to_buckets({0.1: 9.0, 1.0: 9.9, 10.0: 10.0, INF: 10.0})
    assert n == 10.0 and problems == set()
    assert buckets == [
        (-INF, 0.1, 9.0),
        (0.1, 1.0, pytest.approx(0.9)),
        (1.0, 10.0, pytest.approx(0.1)),
    ]


def test_overflow_bucket_is_kept():
    buckets, n, _ = cumulative_to_buckets({1.0: 3.0, INF: 5.0})
    assert buckets[-1] == (1.0, INF, 2.0) and n == 5.0


def test_zero_buckets_are_dropped_but_n_is_kept():
    assert cumulative_to_buckets({1.0: 0.0, INF: 0.0}) == ([], 0.0, set())


def test_non_monotonic_carries_running_max_and_flags():
    buckets, n, problems = cumulative_to_buckets({0.1: 5.0, 1.0: 4.0, INF: 6.0})
    assert problems == {"non_monotonic"}
    assert buckets == [(-INF, 0.1, 5.0), (1.0, INF, 1.0)]
    assert n == 6.0


def test_float_noise_is_not_non_monotonic():
    assert cumulative_to_buckets({0.1: 5.0, 1.0: 5.0 - 1e-12, INF: 5.0})[2] == set()


def test_missing_inf_bucket_flags_lower_bound():
    _, n, problems = cumulative_to_buckets({0.1: 1.0, 1.0: 2.0})
    assert n == 2.0 and problems == {"missing_inf"}


def test_all_nan_column_is_absent():
    assert cumulative_to_buckets({0.1: float("nan"), INF: None}) is None


# shape of Grafana Play query_range, sum by (le) (increase(..._bucket[5m])), trimmed;
# note the lexicographic le order the server returns
CLASSIC = [
    {"metric": {"le": "+Inf"}, "values": [[1790865300, "267.5"], [1790865600, "253.75"]]},
    {"metric": {"le": "0.005"}, "values": [[1790865300, "241.25"], [1790865600, "213.75"]]},
    {"metric": {"le": "0.01"}, "values": [[1790865300, "261.25"], [1790865600, "243.75"]]},
    {"metric": {"le": "10"}, "values": [[1790865300, "267.5"], [1790865600, "253.75"]]},
    {"metric": {"le": "2.5"}, "values": [[1790865300, "267.5"], [1790865600, "253.75"]]},
]


def test_classic_matrix_to_distribution():
    d = from_matrix("play", CLASSIC, expr="e")
    assert d.expr == "e"
    assert d.scheme == BucketScheme("classic", edges=(0.005, 0.01, 2.5, 10.0))
    assert [(c["ts_ms"], c["n"]) for c in d.columns.to_pylist()] == [
        (1790865300000, 267.5),
        (1790865600000, 253.75),
    ]
    first = [
        (r["bucket_lo"], r["bucket_hi"], r["count"])
        for r in d.rows.to_pylist()
        if r["ts_ms"] == 1790865300000
    ]
    assert first == [(-INF, 0.005, 241.25), (0.005, 0.01, 20.0), (0.01, 2.5, 6.25)]
    assert {r["series_id"] for r in d.rows.to_pylist()} == {series_id("play", {})}
    assert "estimated_counts" in d.caveats


def test_series_identity_drops_le_and_vmrange():
    d = from_matrix(
        "s",
        [
            {"metric": {"le": "+Inf", "region": "a"}, "values": [[1, "2"]]},
            {"metric": {"le": "1", "region": "a"}, "values": [[1, "1"]]},
        ],
    )
    assert d.series.to_pylist() == [
        {"series_id": series_id("s", {"region": "a"}), "labels": '{"region":"a"}'}
    ]
    assert d.caveats == ()


def test_classic_scheme_is_the_le_intersection_across_series():
    d = from_matrix(
        "s",
        [
            {"metric": {"le": "1", "r": "a"}, "values": [[1, "1"]]},
            {"metric": {"le": "2", "r": "a"}, "values": [[1, "1"]]},
            {"metric": {"le": "+Inf", "r": "a"}, "values": [[1, "1"]]},
            {"metric": {"le": "1", "r": "b"}, "values": [[1, "1"]]},
            {"metric": {"le": "+Inf", "r": "b"}, "values": [[1, "1"]]},
        ],
    )
    assert d.scheme.edges == (1.0,)


def test_plain_series_is_not_a_histogram():
    with pytest.raises(ValueError, match="not a histogram"):
        from_matrix("s", [{"metric": {"job": "x"}, "values": [[1, "2"]]}])


def test_scheme_round_trip_and_description():
    s = BucketScheme("classic", edges=(0.1, 1.0, 10.0))
    assert BucketScheme.from_dict(s.to_dict()) == s
    assert s.describe() == "classic le buckets: 0.1, 1, 10"
    assert BucketScheme.from_dict(None).kind == "none"


def b(i, schema=3):
    return repr(2 ** (i / 2**schema))


# Grafana Play, sum by (le, vmrange, cloud_region) (increase(traces_spanmetrics_latency{...}[1m])),
# 2026-10-01T10:02Z (bounds are 2^(i/8): schema 3)
NATIVE = [
    {"metric": {"cloud_region": "ap-south-1"}, "histograms": [[1790848920, {
        "count": "8.000800080008", "sum": "212.3",
        "buckets": [[0, b(31), b(32), "2.0"], [0, b(39), b(40), "2.0"], [0, b(40), b(41), "4.0"]]}]]},
    {"metric": {"cloud_region": "eu-west-1"}, "histograms": [[1790848920, {
        "count": "5", "sum": "1.86",
        "buckets": [[0, b(-14), b(-13), "1.25"], [0, b(-12), b(-11), "2.5"], [0, b(-10), b(-9), "1.25"]]}]]},
]  # fmt: skip


def test_native_matrix_uses_histogram_count_and_detects_schema():
    d = from_matrix("play", NATIVE)
    assert d.scheme == BucketScheme("native", schema=3)
    ap = series_id("play", {"cloud_region": "ap-south-1"})
    assert {c["series_id"]: c["n"] for c in d.columns.to_pylist()}[ap] == 8.000800080008
    rows = [
        (r["bucket_lo"], r["bucket_hi"], r["count"])
        for r in d.rows.to_pylist()
        if r["series_id"] == ap
    ]
    assert rows == [
        (2 ** (31 / 8), 16.0, 2.0),
        (2 ** (39 / 8), 32.0, 2.0),
        (32.0, 2 ** (41 / 8), 4.0),
    ]
    assert "estimated_counts" in d.caveats


def test_native_schema_detection():
    assert native_schema([(2 ** (3 / 8), 2 ** (4 / 8)), (2 ** (1 / 4), 2 ** (2 / 4))]) == 2
    assert native_schema([(-1e-128, 1e-128)]) is None  # zero bucket only
    assert native_schema([(0.005, 0.01), (0.01, 0.025)]) is None  # custom (NHCB)
    assert native_schema([(1.0, math.inf)]) is None


def test_mixed_float_and_histogram_samples_are_refused():
    with pytest.raises(ValueError, match="mixes"):
        from_matrix(
            "s", [{"metric": {}, "values": [[1, "1"]], "histograms": NATIVE[0]["histograms"]}]
        )


def test_parse_vmrange():
    assert parse_vmrange("5.275e-02...5.995e-02") == (0.05275, 0.05995)
    assert parse_vmrange("1.000e+18...+Inf") == (1e18, math.inf)
    assert parse_vmrange("0...0") == (0.0, 0.0)
    with pytest.raises(ValueError):
        parse_vmrange("1..2")


# dev VictoriaMetrics: sum by (vmrange) (histogram_over_time(tn_demo_latency_seconds[5m])), step 5m
VMRANGE = [
    {"metric": {"vmrange": "5.275e-02...5.995e-02"}, "values": [[1790865000, "19"]]},
    {"metric": {"vmrange": "5.995e-02...6.813e-02"}, "values": [[1790864700, "10"], [1790865000, "38"]]},
    {"metric": {"vmrange": "6.813e-02...7.743e-02"}, "values": [[1790864400, "46"], [1790864700, "50"], [1790865000, "3"]]},
    {"metric": {"vmrange": "7.743e-02...8.799e-02"}, "values": [[1790864400, "14"]]},
]  # fmt: skip


def test_vmrange_columns_sum_the_present_ranges():
    d = from_matrix("vm", VMRANGE)
    assert d.scheme == BucketScheme("vmrange", per_decade=18)
    assert [(c["ts_ms"], c["n"]) for c in d.columns.to_pylist()] == [
        (1790864400000, 60.0), (1790864700000, 60.0), (1790865000000, 60.0)
    ]  # fmt: skip
    assert d.caveats == ()
