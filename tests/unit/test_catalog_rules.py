import pytest
from hypothesis import given
from hypothesis import strategies as st

from telemetry_nerd.catalog.models import ORIGINS, validate_value
from telemetry_nerd.catalog.rules import (
    PROVENANCE,
    derive_claims,
    facts_from_name,
    normalize_unit,
)
from telemetry_nerd.model.discovery import MetricInfo


def by(claims, origin="rule"):
    return {c.field: c.value for c in claims if c.origin == origin}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "http_requests_total",
            {
                "type": "counter",
                "unit": "count",
                "bounds": "≥0",
                "additivity_series": "additive",
                "additivity_time": "additive",
            },
        ),
        (
            "process_cpu_seconds_total",
            {"type": "counter", "unit": "s", "bounds": "≥0", "additivity_series": "additive"},
        ),
        ("node_memory_bytes", {"unit": "B", "bounds": "≥0", "additivity_series": "additive"}),
        ("lat_seconds_sum", {"unit": "s", "bounds": "≥0", "statistic": "sum"}),
        ("lat_seconds_bucket", {"unit": "s"}),
        ("lat_seconds_count", {"unit": "count", "bounds": "≥0", "statistic": "count"}),
        ("app_latency_p99", {"statistic": "percentile"}),
        ("app_latency_p999", {"statistic": "percentile"}),
        ("request_duration_seconds_p50", {"statistic": "percentile"}),
        (
            "cache_hit_ratio",
            {
                "unit": "ratio",
                "bounds": "[0,1]",
                "additivity_series": "intensive",
                "additivity_time": "intensive",
            },
        ),
        ("cpu_percent", {"unit": "%", "bounds": "≥0", "additivity_series": "intensive"}),
        ("build_info", {"additivity_series": "none"}),
        ("go_gc_duration_nanoseconds_total", {"type": "counter", "unit": "ns", "bounds": "≥0"}),
        ("uptime_milliseconds_total", {"unit": "ms"}),
        ("node_cpu_frequency_hertz", {"unit": "Hz", "bounds": "≥0"}),
        ("node_hwmon_temp_celsius", {"unit": "°C"}),
        ("kepler_energy_joules_total", {"unit": "J", "type": "counter"}),
        ("power_watts", {"unit": "W", "bounds": "≥0"}),
        ("http.server.duration_seconds", {"unit": "s", "bounds": "≥0"}),
        ("traces_spanmetrics_latency", {"unit": "s", "type": "histogram", "bounds": "≥0"}),
        ("traces_service_graph_request_server_seconds_sum", {"unit": "s"}),
    ],
)
def test_rule_table(name, expected):
    got = by(derive_claims(name))
    assert {k: got[k] for k in expected} == expected


@pytest.mark.parametrize("name", ["up", "queue_depth", "node_load1", "process_open_fds"])
def test_unknown_names_get_no_claims(name):
    assert derive_claims(name) == []


def test_plain_seconds_or_bytes_gets_no_type_guess():
    assert "type" not in by(derive_claims("request_seconds"))
    assert "type" not in by(derive_claims("memory_bytes"))


def test_metadata_claims_and_unit_normalization():
    info = MetricInfo("m", "gauge", "How long it takes", "seconds")
    got = by(derive_claims("m", info), "metadata")
    assert got == {"type": "gauge", "unit": "s", "description": "How long it takes"}
    assert normalize_unit("By") == "B" and normalize_unit("percent") == "%"
    assert normalize_unit("Hz") == "Hz" and normalize_unit("Cel") == "°C"
    assert normalize_unit("1") is None and normalize_unit("{request}") is None
    assert normalize_unit(None) is None


def test_missing_metadata_makes_no_metadata_claims():
    assert by(derive_claims("up", MetricInfo("up")), "metadata") == {}
    assert by(derive_claims("up", None), "metadata") == {}


def test_classic_family_claims_on_base_and_members():
    fam = {"lat_seconds": "classic"}
    members = ["lat_seconds_bucket", "lat_seconds_sum", "lat_seconds_count"]
    for name in ("lat_seconds", *members):
        assert by(derive_claims(name, None, fam))["histogram_family"] == members
    assert by(derive_claims("lat_seconds_bucket", None, fam))["type"] == "counter"
    # without discovery a _bucket name alone is not enough to claim a type
    assert "type" not in by(derive_claims("lat_seconds_bucket"))


def test_native_family_is_just_the_base():
    claims = derive_claims(
        "native_lat", MetricInfo("native_lat", "histogram"), {"native_lat": "native"}
    )
    assert by(claims)["histogram_family"] == ["native_lat"]
    assert by(claims, "metadata")["type"] == "histogram"


def test_facts_from_name_keeps_old_suffix_behavior():
    f = facts_from_name("tn_demo_latency_seconds_sum")
    assert (f.unit, f.type, f.unit_provenance) == ("s", None, "inferred from metric name")
    assert facts_from_name("up").unit is None
    assert facts_from_name("x_calls_total").type == "counter"


def test_every_origin_has_a_provenance_label():
    assert set(PROVENANCE) == set(ORIGINS)


# -- statistic / mergeability T0 rules (telemetry-nerd-2as.20, spec §5 [H]/[SfE]) ------------


def test_summary_base_series_is_a_precomputed_percentile():
    info = MetricInfo("lat_seconds", "summary", "request latency", "seconds")
    got = by(derive_claims("lat_seconds", info))
    assert got["statistic"] == "percentile"


def test_summary_sum_and_count_members_stay_mergeable():
    sum_info = MetricInfo("lat_seconds_sum", "summary")
    count_info = MetricInfo("lat_seconds_count", "summary")
    assert by(derive_claims("lat_seconds_sum", sum_info))["statistic"] == "sum"
    assert by(derive_claims("lat_seconds_count", count_info))["statistic"] == "count"


@pytest.mark.parametrize(
    "name", ["service_latency_p99", "service_latency_p95", "service_latency_p999", "x_p50"]
)
def test_percentile_gauge_naming_convention(name):
    assert by(derive_claims(name))["statistic"] == "percentile"


def test_non_percentile_names_get_no_statistic_claim():
    assert "statistic" not in by(derive_claims("up"))
    assert "statistic" not in by(derive_claims("node_load1"))
    assert "statistic" not in by(derive_claims("http_requests_total"))


@given(st.text(max_size=40))
def test_rules_only_emit_valid_claims(name):
    for c in derive_claims(name, MetricInfo(name, "gauge", "h", "bytes"), {name: "classic"}):
        validate_value(c.field, c.value)
        assert 0 <= c.confidence <= 1


def test_info_metrics_get_no_type_claim():
    # Prometheus exposes *_info as a gauge; a rule-claimed "info" contradicted declared metadata
    assert "type" not in by(derive_claims("kube_pod_info"))
