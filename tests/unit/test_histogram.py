import math

import pytest

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.analysis.histogram import cumulative_to_buckets, from_matrix, histogram_expr
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
