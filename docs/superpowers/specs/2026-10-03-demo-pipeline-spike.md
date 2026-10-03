# Demo pipeline spike: OTLP vs remote write into VictoriaMetrics

Bead `telemetry-nerd-1h9.3` (spec §4.4). Run 2026-10-03 against VictoriaMetrics v1.137.0,
OpenTelemetry Collector contrib 0.160.0 and the OpenTelemetry Demo 3.1.0 (the stack in
`deploy/demo`). Question: does metric metadata (TYPE/HELP/UNIT) survive the Collector ->
VictoriaMetrics hop, what do names and histograms look like, and which path should the demo use?

## Setup

One Collector (`deploy/demo/otelcol-spike.yml`) fans the same metrics out to three VMs:

| VM | Port | Path |
|---|---|---|
| `victoriametrics` | 8429 | `otlphttp` -> `/opentelemetry/v1/metrics`, VM defaults |
| `vm-otlp-prom` | 8442 | same, VM started with `-opentelemetry.usePrometheusNaming` |
| `vm-rw` | 8441 | `prometheusremotewrite` -> `/api/v1/write`, `resource_to_telemetry_conversion` on |

Reproduce: `OTELCOL_CONFIG=otelcol-spike.yml just demo-up spike`, wait a few minutes. The SDKs
export cumulative temporality and explicit-bucket histograms (the demo's `.env` defaults).

## Findings

**F1. Only OTLP carries metadata.** `GET /api/v1/metadata` after 5 minutes:

```
OTLP default      138 metrics  {"http.server.request.duration": [{"type":"histogram","unit":"s","help":"Duration of HTTP server requests."}]}
OTLP prom-naming  140 metrics  {"http_server_active_requests": [{"type":"gauge","unit":"{request}","help":"Number of active HTTP server requests."}],
                                "process_cpu_time_seconds_total": [{"type":"counter","unit":"s","help":"Total CPU seconds broken down by different states."}],
                                "jvm_memory_used_bytes": [{"type":"gauge","unit":"By","help":"Measure of memory used."}]}
remote write        0 metrics  {}
```

TYPE is right for counters, gauges and histograms; HELP survives where the SDK set one (136 of
140); UNIT is the OTel/UCUM unit as declared (`s`, `By`, `ms`, `{request}`), not Prometheus
base units. The Collector's remote-write v1 exporter sends no metadata at all, so VM has none
to store (VM's `-enableMetadata` is on by default and accepts it from remote write; it just
never arrives).

**F2. Naming.** Default OTLP ingest keeps OTel names and attribute keys verbatim, with dots:
metric `http.server.request.duration_bucket`, labels `service.name`, `http.response.status_code`,
no unit suffix and no `_total`. MetricsQL accepts dotted names, but `by (service.name)` and
every PromQL-flavoured tool (our knowledge packs, Grafana Play dashboards) do not. With
`-opentelemetry.usePrometheusNaming` VM applies the Prometheus translation: unit suffix
(`http_server_request_duration_seconds_bucket`), `_total` on monotonic sums, `_ratio` on unit
`1` gauges, dots become `_` in names and labels (`service_name`). The remote-write path gives
the same names (the exporter does the translation) except for a few unit quirks, and adds
`job`/`instance` plus a `target_info` series; OTLP ingest adds neither (resource attributes
become labels directly, nothing is dropped).

Counts of distinct metric names: 445 (default; includes VM's own `vm_*`/`go_*` self-scrape),
162 (prom naming), 161 (remote write).

**F3. Histograms.** Explicit-bucket histograms arrive as classic `_bucket{le}`/`_sum`/`_count`
on every path. Exponential histograms (probe sent through the Collector as OTLP JSON, scale 0,
10 observations):

```
OTLP (both VM modes)  spike_latency_seconds_bucket{vmrange="1.000e+00...2.000e+00"} ... + _sum/_count
remote write          nothing: series list is empty (the exporter drops exponential histograms)
```

OTLP converts them to VictoriaMetrics' `vmrange` buckets (log-uniform, 18 per decade), which
the Telemetry Nerd adapter already understands (`SchemeKind.vmrange`). Remote write loses them
silently. The demo SDKs are pinned to explicit buckets, so this only matters for external data.

**F4. Telemetry Nerd on each.** `source_learn` + `binding_suggest kind=RED` against the first
two VMs (daemon from this branch):

| VM | metrics learned | RED suggestions |
|---|---|---|
| OTLP default | 526 (dotted names, no semconv pack match, vm_* noise) | only VM's own `vm_http_*` |
| OTLP prom-naming | 228 | `RED:otel_http` (http_server), `RED:otel_rpc` (rpc_server), `RED:spanmetrics`, join on `service_name` |

The knowledge pack (`otel_semconv.toml`) and the binding rules assume Prometheus-style names.

**F5. Exporter gotchas.** Collector 0.160 renames components (`otlphttp` -> `otlp_http`,
`spanmetrics` -> `span_metrics`, `prometheusremotewrite` -> `prometheus_remote_write`,
`resource_to_telemetry_conversion` -> `resource_constant_labels`); the old names still work with
a deprecation warning. The production config uses the new ones.

**F6. VictoriaMetrics OTLP ingest bug (v1.137.0), affects prom-naming mode.** A metric whose
unit is empty inherits the unit of the metric before it in the same request. Symptoms: the
span-metrics `calls` counter came out as `traces_span_metrics_calls_milliseconds_total` for
every service except the first in each request (`..._calls_total` for the rest), and gauges
like `process_thread_count_bytes`, `system_thread_count_connections` appeared. Confirmed by the
fix: with a Collector `transform` that gives empty units an annotation unit
(`set(metric.unit, "{unspecified}") where metric.unit == ""`; annotations are kept as metadata
unit but never added to the name), all of `traces_span_metrics_calls_total` is named
consistently and the odd suffixes are gone (`process_thread_count`, `system_thread_count`).
Worth reporting upstream.

## Decision

**OTLP/HTTP from the Collector into VictoriaMetrics' native OTLP endpoint, VM started with
`-opentelemetry.usePrometheusNaming`, plus the empty-unit transform.** Reasons: only OTLP keeps
TYPE/HELP/UNIT (F1) which learning uses for unit/type claims; Prometheus naming makes the
semconv pack and binding suggestions work (F4) and the names match what Grafana Play (Mimir)
exposes for the same demo; exponential histograms are not lost (F3). Costs: the UNIT is UCUM
(`By`, `s`, `{request}`), and the F6 workaround.

Rejected: remote write (no metadata, drops exponential histograms) and default-naming OTLP
(dotted names break PromQL and the packs).

## Demo footprint (default profile, 20 Locust users)

About 1.0-1.2 GiB resident for the 17 containers after 10 minutes, ~5 GB of images; see
`docs/demo.md` (Sizes). On the final stack (OTLP + prom naming + empty-unit transform) a fresh
VM learned 222 metrics and `binding_suggest` proposed `RED:otel_http`, `RED:otel_rpc` and
`RED:spanmetrics` joined on `service_name`.
