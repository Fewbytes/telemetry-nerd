"""Collector and relabel configs as context (bead 2as.27)."""

import pytest

from telemetry_nerd.catalog.context import extract
from telemetry_nerd.catalog.context_yaml import NameTransform, variants
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import FakeSource, make_service

COLLECTOR = """\
receivers:
  otlp: {protocols: {grpc: {}}}
processors:
  metricstransform/rename:
    transforms:
      - include: http.server.duration
        action: update
        new_name: http.server.request.duration
        operations:
          - action: experimental_scale_value
            experimental_scale: 0.001
      - include: ^queue\\.(.*)$
        match_type: regexp
        action: update
        new_name: jobs.queue.$${1}
  transform:
    metric_statements:
      - context: metric
        statements:
          - set(unit, "s") where name == "db.client.latency"
          - set(name, "cache.hit.ratio") where name == "cache.ratio"
          - delete_key(attributes, "x")
exporters:
  prometheus:
    namespace: acme
  prometheusremotewrite:
    endpoint: http://x
    add_metric_suffixes: false
service: {}
"""
PROM = """\
scrape_configs:
  - job_name: app
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: "app_(.*)"
        target_label: __name__
        replacement: "acme_$1"
      - source_labels: [env]
        action: drop
        regex: dev
"""
GO_CODE = """\
package m
func init() {
    meter.Float64Histogram("http.server.duration", metric.WithUnit("ms"), metric.WithDescription("Server latency."))
}
"""


def test_collector_rules_are_read_and_what_is_not_understood_is_reported():
    out = extract([{"path": "otel.yaml", "text": COLLECTOR}])
    kinds = [(t.kind, t.old, t.new, t.scale, t.regex) for t in out.transforms]
    assert ("rename", "http.server.duration", "http.server.request.duration", 0.001, False) in kinds
    assert any(k[0] == "rename" and k[4] and k[1] == r"^queue\.(.*)$" for k in kinds)
    assert ("unit", "db.client.latency", None, None, False) in kinds
    assert ("rename", "cache.ratio", "cache.hit.ratio", None, False) in kinds
    assert [t.value for t in out.transforms if t.kind == "prefix"] == ["acme"]
    assert [t.value for t in out.transforms if t.kind == "suffixes"] == [False]
    [bad] = out.skipped
    assert "OTTL not understood" in bad.reason and "delete_key" in bad.reason
    assert all("otel.yaml#" in t.path for t in out.transforms)


def test_prometheus_relabel_renames_are_read_and_other_rules_ignored():
    out = extract([{"path": "prometheus.yml", "text": PROM}])
    [t] = out.transforms
    assert (t.kind, t.old, t.new, t.stage) == ("rename", "app_(.*)", "acme_$1", "prom")
    assert t.path == "prometheus.yml#scrape_configs[0].metric_relabel_configs[0]"


def test_unreadable_or_unrelated_yaml_is_skipped_with_a_reason():
    bad = extract([{"path": "a.yaml", "text": "a: [unclosed"}])
    assert "invalid YAML" in bad.skipped[0].reason
    other = extract([{"path": "b.yml", "text": "name: x\nreplicas: 3\n"}])
    assert "no collector or relabel rules" in other.skipped[0].reason
    assert (
        extract([{"path": "c.yaml", "text": "- a\n- b\n"}]).skipped[0].reason
        == "YAML that is not a mapping"
    )


def otel_def(path="m.go"):
    return extract([{"path": path, "text": GO_CODE}]).definitions[0]


def rules(**kw):
    return extract([{"path": "otel.yaml", "text": COLLECTOR}]).transforms


def test_each_exporter_is_one_way_the_pipeline_could_carry_the_metric():
    d = otel_def()
    both = variants(d, rules())
    # prometheus exporter (namespace acme) and remote write (no suffixes) are separate candidates
    assert [v.names[0] for v in both] == [
        "acme_http_server_request_duration_milliseconds_bucket",
        "http_server_request_duration_bucket",
    ]
    first = both[0]
    assert (
        "collector metricstransform otel.yaml#processors.metricstransform/rename.transforms[0]"
        in first.how
    )
    assert "exporter otel.yaml#exporters.prometheus.namespace" in first.how
    # values were scaled by 0.001 but the name still says milliseconds: no unit is claimed
    assert first.unit is None and both[1].unit is None


def test_a_stated_unit_is_the_configs_own_word():
    rename = NameTransform(
        "rename",
        "x#r",
        "collector metricstransform",
        "http.server.duration",
        "http.server.request.duration",
        scale=0.001,
    )
    unit = NameTransform("unit", "x#u", "collector transform", "http.server.duration", value="s")
    [v] = variants(otel_def(), [rename, unit])
    assert v.names[0] == "http_server_request_duration_seconds_bucket" and v.unit == "s"


def test_suffixes_off_drops_unit_and_total_suffixes():
    d = otel_def()
    only = [t for t in rules() if t.kind == "suffixes"]
    [v] = variants(d, only)
    assert v.unit == "ms"  # nothing was scaled: the code's unit stands
    assert v.names == (
        "http_server_duration_bucket",
        "http_server_duration_sum",
        "http_server_duration_count",
    )


def test_a_scale_that_is_no_canonical_unit_drops_the_unit_instead_of_guessing():
    t = NameTransform(
        "rename",
        "x#t",
        "collector metricstransform",
        "http.server.duration",
        "http.server.duration",
        scale=7.0,
    )
    [v] = variants(otel_def(), [t])
    assert v.unit is None


def test_regexp_rename_and_relabel_work_on_names_and_nothing_else_matches():
    prom_def = extract(
        [
            {
                "path": "app.py",
                "text": 'from prometheus_client import Counter\nC = Counter("app_jobs", "Jobs.")\n',
            }
        ]
    ).definitions[0]
    assert prom_def.names == ("app_jobs_total",)
    relabel = extract([{"path": "prometheus.yml", "text": PROM}]).transforms
    [v] = variants(prom_def, relabel)
    assert v.names == ("acme_jobs_total",) and "prometheus relabel" in v.how
    assert variants(prom_def, rules()) == []  # the collector's namespace is for OTLP metrics only
    assert variants(prom_def, []) == []


# --- through catalog_context ------------------------------------------------------------------
@pytest.fixture
async def svc(tmp_path):
    infos = [
        MetricInfo("acme_http_server_request_duration_seconds_bucket"),
        MetricInfo("acme_http_server_request_duration_seconds_sum"),
        MetricInfo("acme_http_server_request_duration_seconds_count"),
        MetricInfo("acme_http_server_request_duration_milliseconds_bucket"),
        MetricInfo("acme_http_server_request_duration_milliseconds_sum"),
        MetricInfo("acme_http_server_request_duration_milliseconds_count"),
        MetricInfo("acme_jobs_total", "counter"),
    ]
    d = Discovery(tuple(infos), (), {}, None, 1.0, (), False)
    s = make_service(tmp_path, FakeSource(name="default", discovery=d))
    await s.learn("default")
    return s


def claim(svc, metric, field):
    return next(
        (
            c
            for c in svc.ws.catalog_entry("default", metric).claims.get(field, [])
            if c.origin == "context"
        ),
        None,
    )


COLLECTOR_SECONDS = """\
processors:
  metricstransform:
    transforms:
      - include: http.server.duration
        action: update
        new_name: http.server.request.duration
        operations:
          - action: experimental_scale_value
            experimental_scale: 0.001
  transform:
    metric_statements:
      - context: metric
        statements:
          - set(unit, "s") where name == "http.server.duration"
exporters:
  prometheus:
    namespace: acme
"""


async def test_a_renamed_series_still_gets_the_codes_description_and_the_configured_unit(svc):
    files = [
        {"path": "server.go", "text": GO_CODE},
        {"path": "otel.yaml", "text": COLLECTOR_SECONDS},
    ]
    out = svc.ws.catalog_context("default", files)
    assert out["unmatched"] == [] and out["matched_through_pipeline"] == 1
    assert out["pipeline_rules"] == 3
    m = "acme_http_server_request_duration_seconds_sum"
    d, u = claim(svc, m, "description"), claim(svc, m, "unit")
    assert d.value == "Server latency." and u.value == "s"
    assert d.confidence == pytest.approx(0.80)  # a longer inference than a plain registration
    assert "go opentelemetry" in d.citation and "through collector metricstransform" in d.citation


async def test_a_scaled_unit_that_the_name_contradicts_is_not_claimed(svc):
    files = [{"path": "server.go", "text": GO_CODE}, {"path": "otel.yaml", "text": COLLECTOR}]
    out = svc.ws.catalog_context("default", files)
    assert out["unmatched"] == []
    m = "acme_http_server_request_duration_milliseconds_sum"
    assert claim(svc, m, "description").value == "Server latency."
    assert claim(svc, m, "unit") is None


async def test_without_the_collector_config_the_same_code_is_unmatched(svc):
    out = svc.ws.catalog_context("default", [{"path": "server.go", "text": GO_CODE}])
    assert [u["name"] for u in out["unmatched"]] == ["http_server_duration_milliseconds"]
    assert out["matched_through_pipeline"] == 0


async def test_relabelled_prometheus_names_match_through_the_rule(svc):
    code = 'from prometheus_client import Counter\nC = Counter("app_jobs", "Jobs run.")\n'
    out = svc.ws.catalog_context(
        "default",
        [{"path": "app.py", "text": code}, {"path": "prometheus.yml", "text": PROM}],
    )
    assert out["unmatched"] == [] and claim(svc, "acme_jobs_total", "type").value == "counter"
