# Spike: public Grafana/Prometheus instances as test sources (telemetry-nerd-1h9.1)

- **Date:** 2026-10-01
- **Status:** Done — findings and recommendations
- **Probe scripts:** sequential, ≤1 req/s, descriptive User-Agent; ~150 requests total
- **Scope:** deep dive on Wikimedia (first part); survey of other public instances in
  [Other public sources](#other-public-sources) — from
  <https://grafana.com/blog/worth-a-look-public-grafana-dashboards/> plus known demos.

All Grafana-fronted sources follow one pattern: `GET <grafana>/api/frontend/settings`
lists datasources (anonymous orgs only); query through
`<grafana>/api/datasources/proxy/uid/<uid>/<native API path>`. Listed ≠ queryable: the
datasource's own credentials may still deny some statements (see CERN InfluxDB).

# Wikimedia


## What is there

`grafana.wikimedia.org` has anonymous access enabled. `/api/frontend/settings` lists the
datasources; each is reachable through Grafana's proxy:

```
https://grafana.wikimedia.org/api/datasources/proxy/uid/<uid>/api/v1/...
```

| Datasource | uid | Backend |
|---|---|---|
| `thanos` (global, all sites) | `000000026` | Thanos 0.38 |
| `thanos-downsample-5m` | `P1B4DE8CFE4C343FA` | Thanos 0.38 |
| `thanos-downsample-1h` | `PA7DE9A562EF40E24` | Thanos 0.38 |
| `<site> prometheus/<role>` (e.g. eqiad ops `000000006`, eqiad k8s `000000017`) | various | Prometheus 2.48.1 |

Label `prometheus` ∈ analytics, cloud, ext, k8s, k8s-aux, k8s-dse, k8s-mlserve,
k8s-mlstaging, k8s-staging, ops, services. Label `site` ∈ codfw, drmrs, eqiad, eqsin,
esams, magru, ulsfo. Also Graphite and Loki ("Public Logs") — out of scope for now.

Responses pass through Varnish (`x-cache: … miss, pass`): nothing is cached for us.

## Findings

### Scale

- **93.7k metric names** (`/label/__name__/values`, 8.7 MB, ~3 s).
- **62% of names are Airflow** (57.8k): dimensions encoded in the metric *name*
  (statsd-exporter style). Then mobileapps 5k, swift 3.9k, jgit 2.3k, cassandra 2k, …
  node 1.1k, mediawiki 1k, envoy 536, kube 162.
- Per-site Prometheus head: **13M series** (eqiad ops), **20M** (eqiad k8s).
  Top cardinality: `node_systemd_unit_state` 1.07M, `istio_*_bucket` 0.9M each,
  `envoy_*_bucket` 0.4–0.8M.

### Metadata

- `/metadata` on Thanos: 13–20 MB, ~4–5 s. **Not deterministic:** two calls minutes
  apart returned 88,837 vs 48,334 metrics. Thanos fans out to stores; incomplete
  results are silent.
- ~6.6k–48k names have no metadata at all (depends on the call).
- Type mix (larger call): counter 56.8k, gauge 21.7k, unknown 8.4k, summary 1.2k,
  histogram 692, info 3.
- **`unit` is empty for all but 13 metrics.** Units must come from T0 naming rules and
  packs, not metadata.

### Queries

- Standard Prometheus limit: **11,000 points per series** (HTTP 400 beyond it). Matches
  `MAX_STEPS_PER_QUERY`.
- Narrow queries are fast: 1 series × 24h × 60s ≈ 0.3–0.6 s; × 90d × 1h ≈ 0.4–1 s.
- Broad queries are expensive: `max_over_time(up[5m])` over 1h = 26 MB, 9–27 s.
  `count by (__name__)({__name__=~".+"})` **timed out** (>60 s client side). Prometheus
  flags: `query.timeout=2m`, `query.max-samples=10M`.
- Raw-sample queries (`up{job="node"}[2m]`) show **heterogeneous scrape intervals** —
  mostly 60 s, also 15/20/30 s and jitter. The series interval is a per-job/per-series
  property, not per-source.
- Latency varies run to run (same query 1 s vs 30 s): shared, loaded infrastructure.

### Retention

- Thanos: data 365 d ago present; 730 d ago absent → **≥1 year**. Downsampled 5m/1h
  datasources answer 90 d × 1h quickly — the right source for operating profiles.
- Per-site Prometheus: `retention.time=1000d`, size-capped at ~3.2 TiB.

### Cardinality without scanning

- Per-site Prometheus exposes **`/api/v1/status/tsdb`** (top series counts by metric
  name, label-pair counts) in ~0.5 s. Thanos does not. Cheap cardinality source.
- `/api/v1/series?match[]=<metric>` is fine per metric (MemTotal: 347 KB, ~1 s) but not
  for 94k metrics.

### Politeness

- `robots.txt` is `Disallow: /` (crawlers). We are not crawling, but this is a public
  service of a non-profit. Treat it as **interactive-volume only**.
- No rate-limit headers observed. Wikimedia's User-Agent policy asks for a descriptive UA
  with contact info.

## Recommendations

1. **Use it, interactively.** Thanos `000000026` for investigations, `thanos-downsample-1h`
   for operating profiles. Prometheus flavor (no MetricsQL `rollup()`), so the
   `*_over_time` fallback path. Per-site Prometheus only for `/status/tsdb`.
2. **Client politeness is part of source config:** descriptive UA, max concurrency 1–2,
   min interval between requests, backoff on 5xx/timeout, per-request timeout ≥60 s
   (server allows 2 min). Never run broad scans (`{__name__=~".+"}`, `count by
   (__name__)`).
3. **Discovery (2as.2) must be cheap and honest:**
   - names from `/label/__name__/values` (one call), metadata from `/metadata` — retry and
     union, record `metadata_coverage` as a caveat; missing metadata ≠ "no type";
   - cardinality from `/status/tsdb` where available, else lazily per metric on first use;
   - series interval inferred per metric/job lazily (raw-sample instant query);
   - cluster name-encoded dimensions (Airflow) into name templates before T0/T2, or the
     catalog becomes 58k near-duplicate entries.
4. **T1 sample stats budgeted and lazy** on a source like this: hot metrics and requested
   families only, never all 94k.
5. **Tests replay recorded fixtures** (record once with the polite client, commit trimmed
   JSON under `tests/fixtures/wikimedia/`); no network in CI. Small: a handful of
   node_exporter/envoy/mediawiki metrics, a `/metadata` slice, a `/status/tsdb` response.
6. **Demo material:** node_exporter (memory/fs/net → bounded_by context, 2as.15),
   envoy/istio histograms (heatmaps, M4), mediawiki/varnish request rates (RED),
   Airflow (name-template clustering).

## Live verification of operating profiles (telemetry-nerd-2as.23, 2026-10-02)

`ProfileService` run end to end against live Wikimedia (1 request at a time, >= 1 s apart),
`wikimedia` -> `wikimedia-1h` pairing: `profiled_from` was `wikimedia-1h` for all three; the
unpaired source was not contacted. Fixtures: `tests/fixtures/wikimedia/profile/` (110 KiB,
13 files: 12 `query_range` + `clock.json`; recorder `scripts/record_wikimedia_profiles.py`),
replayed offline by `tests/unit/test_wikimedia_profiles.py`; live test
`tests/integration/test_wikimedia_profile_live.py`.

| Profile (30 d x 1h, 720 h) | Result | Wall time (cold) |
|---|---|---|
| gauge `node_load1{wdqs1018}` (selector: min/max/avg/count rollup) | n=720, p50 55.0, p0.5-p99.5 0.3-98, envelope 0-133.6, max 136.6, seasonal fit, no caveats | 7.6-9.2 s |
| counter `node_network_receive_bytes_total{eno12399np0}` -> `rate(x[4h])` | n=720, p50 160 kB/s, p0.5-p99.5 17-244 kB/s; `no_intra_hour_extremes` | 2.0-2.4 s |
| `histogram_quantile(0.99, ...envoy_cluster_upstream_rq_time_bucket...)` | n=720, p50 45 ms, p99.5 2500 (the top finite bucket); `quantile_series` | 1.6-1.8 s |

Per-query latency of the 30 d x 1h range queries (min/max/avg/count_over_time(x[1h]),
rate(x[4h]), histogram_quantile): 0.3-0.8 s, 15-25 KB responses, on both datasources. The
gauge's 7.6 s is 8 requests (4 rollups x 2 cache chunks) serialized by the politeness gate.

**The `thanos-downsample-1h` datasource does not serve downsampled data by default.** At 20,
60, 120, 200 and 300 days back, `node_load1` / `max_over_time(x[5m])` at 5 min steps and
`count_over_time(x[1h])` are identical to the raw `thanos` datasource (same values, count 60,
the raw series interval); Wikimedia keeps raw data for >= 300 days, and the downsample datasource
only *allows* coarser blocks (Thanos `max_source_resolution`), it does not prefer them.
Sending `max_source_resolution=1h` explicitly does return downsampled blocks (1h-spaced
samples): hourly min/max/avg then match raw within ~0.3-0.6% (median relative difference;
block boundaries differ slightly), rate(x[4h]) and the quantile within ~0.1%, but
`count_over_time(x[1h])` returns about 1 (the downsampled sample count; the sample count is
lost), with no latency gain. Consequences: the profile's hourly min/max on Wikimedia are the
true min/max at the series interval (raw), the pairing is a functional no-op there, and the
"downsampled data is faster" premise did not hold: raw Thanos already answers 30 d x 1h in
under a second. Decision on the preset: telemetry-nerd-2as.25.

Also noted: the series cache fetches whole 720-bucket chunks aligned to the epoch, and a 30 d
x 1h profile window is exactly one chunk long, so a cold profile reads two chunks (about 2x the
needed data). Harmless here; later daily refreshes fetch only the newest chunk.


# Other public sources

Probed 2026-10-01 with one or two light requests each. "Unit coverage" = metadata entries
with a non-empty `unit`.

## Prometheus-compatible (in scope for M3)

| Source | Base URL (append `/api/v1/...`) | Backend | Names | Unit coverage | Contents | Use for |
|---|---|---|---|---|---|---|
| Wikimedia Thanos | `https://grafana.wikimedia.org/api/datasources/proxy/uid/000000026` | Thanos 0.38 | 93.7k | 13 | Production infra (see above) | Scale, discovery stress, history ≥1y |
| Grafana Play | `https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom` | Grafana Cloud (Mimir) | 3.5k | — | **Hosted OpenTelemetry Demo** (`ecommerce-prod/*` incl. flagd, kafka), banking microservices demo (`banking-prod/*`), QuickPizza, k8s (KSM, cadvisor, kubelet), MySQL/PG/Redis/Kafka exporters, synthetic checks; 118 jobs | Microservices/RED without local setup |
| CERN EOS | `https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/b490b4f4-28d2-4f0b-8fe1-b0bb61c365f6` | Thanos 0.32.5 | 1.5k | 0/1.2k | EOS storage, xrootd, cernbox, node_exporter | Storage domain, Thanos |
| CERN OpenStack | `https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/bf9ylnkygnnr4c` | Mimir 2.15 | 1.9k | 1/1.8k | OpenStack, RabbitMQ, HAProxy, k8s, libvirt, argocd | Mimir, cloud infra |
| CERN DBoD | `https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/ff1pt73d7olxcc` | Mimir 2.15 | 5.1k | 0/3.8k | Nomad, Consul, MySQL, Postgres (database-on-demand) | Databases, name-encoded families |
| Percona PMM demo | `https://pmmdemo.percona.com/graph/api/datasources/proxy/uid/PA58DA793C7250F1B` | VictoriaMetrics (`rollup()` works) | 8.3k | 0/7.8k | MongoDB, MySQL, ClickHouse, PG, ProxySQL, Redis, node | MetricsQL path, databases |
| VictoriaMetrics playground | `https://play.victoriametrics.com/select/0/prometheus` | VictoriaMetrics cluster | 1.7k | 255/1.6k | VM self-monitoring, node, KSM, kafka, otelcol | MetricsQL path, best metadata |
| Prometheus demo | `https://prometheus.demo.prometheus.io` | Prometheus 3.13 | 1.4k | 0/1.1k | node, cadvisor, blackbox, grafana, caddy, `random`; 11 targets | Small stable fixtures |
| PromLabs demo | `https://demo.promlabs.com` | Prometheus 3.14 | 683 | 40/590 | node, cadvisor, `demo` API service (latency histograms) | Small fixtures, histograms |

All four backend families are covered: Prometheus, Thanos, Mimir, VictoriaMetrics.
Grafana Play's hosted OTel demo covers microservices realism, but we cannot toggle its
flagd flags, so scenario evals with ground truth still need the local demo (1h9.4/1h9.5).

## Not usable

| Source | Result |
|---|---|
| GitLab `dashboards.gitlab.com` / `.net` | 401, login required |
| Arch Linux `monitoring.archlinux.org` | 401 |
| GridKa (KIT) `grafana-sdm.scc.kit.edu` | 401 |
| CNCF DevStats `devstats.cncf.io` | no Grafana API exposed |
| Zabbix demo `play.grafana-zabbix.org`, linksmart demo | no usable API |
| Fedora `stats.fedoraproject.org` | unreachable |

## Other datasource types (for later adapters: logs, InfluxDB, Elasticsearch)

Access status from one probe each; "listed" = in frontend settings, not probed.

**CERN** (`https://monit-grafana-open.cern.ch`, proxy `/api/datasources/proxy/uid/<uid>`)

| Type | Name | uid | Index / database | Time field | Access |
|---|---|---|---|---|---|
| elasticsearch | esnet | `000007855` | `esnet_*` | `timestamp` | `_mapping` OK (perfSONAR: owd, packet loss, throughput) |
| elasticsearch | monit_es_fts_agg | `TTlBPNPMk` | `monit_prod_fts_agg*` | `metadata.timestamp` | `_mapping` OK |
| elasticsearch | monit_es_wlcg_agg | `Y_GIEHEMk` | `monit_prod_wlcg_agg*` | `metadata.timestamp` | listed |
| elasticsearch | monit_es_wlcg_sitenetwork | `s0TUbLzIz` | `monit_prod_wlcg_raw_sitenetwork*` | `metadata.timestamp` | listed |
| elasticsearch | monit_fts_enr_complete | `Ma6YTHEGk` | `monit_prod_fts_enr_complete*` | `metadata.event_timestamp` | listed |
| elasticsearch | monit_lhcopn | `000007670` | `monit_prod_lhcopn_*` | `metadata.timestamp` | listed |
| elasticsearch | monit_openstack | `ecVkK187k` | `monit_private_openstack_logs_*` | `metadata.timestamp` | listed (logs) |
| elasticsearch | monit_os_wlcgops-pledge | `d0c7bc0c-5ec7-41c8-be11-85b056907611` | `monit_prod_wlcgops_agg_accounting.pledged-*` | `metadata.timestamp` | listed |
| elasticsearch | monit_os_wlcgops-req | `a6f60902-46a9-4bb2-b73e-4650a1a13ede` | `monit_prod_wlcgops_agg_accounting.required-*` | `metadata.timestamp` | listed |
| elasticsearch | monit_prod_eosc-future_raw-lt | `s8OlM7j4z` | `monit_prod_eosc-future_raw*` | `metadata.timestamp` | listed |
| elasticsearch | mwt2_ps_{owd,packet_loss,retransmits,throughput,trace} | `000008394`–`000008398` | `ps_owd`, `ps_packetloss`, `ps_retransmits`, `ps_throughput`, `ps_trace` | `timestamp` | listed (perfSONAR) |
| opensearch 3.4 | monit_os_wlcgops_accounting_space_operations | `cfluj1ei1y3nkb` | `monit_prod_wlcgops_agg_accounting.space.operations-*` | `metadata.timestamp` | `_mapping` 403; search untested |
| opensearch 3.4 | monit_os_wlcgops_accounting_wau | `eflqbrokj6e4gb` | `…accounting.wau.summary-*` | `metadata.timestamp` | listed |
| opensearch 3.4 | monit_os_wlcgops_cpu_pledges | `dflu8t0tb2juob` | `…accounting.cpu.pledges-*` | `metadata.timestamp` | listed |
| opensearch 3.4 | monit_os_wlcgops_space_pledges | `aflu92dj0ighse` | `…accounting.space.pledges.30d-*` | `metadata.timestamp` | listed |
| influxdb | monit_idb_{availability,batch,cloud,dcbynumbers,fts,landb,netstat,tape,transfers} | `000009334`, `000009636`, `BO3SrCOGk`, `NF6Os9hGk`, `000007669`, `000009669`, `OBxSIfanz`, `000009533`, `000007856` | `monit_production_<name>` | — | `SHOW MEASUREMENTS` 403 (needs READ); `SELECT` via `/api/ds/query` untested |
| influxdb | monit_idb_collectd_dcops, monit_idb_wlcgops | `e53a6ee7-cc1c-4636-8ed2-c92dccdfca91`, `f711a5a9-2d3b-4fe9-8147-3676655008c5` | — | — | listed |
| influxdb | openstack_influxdb, openstack_rally_influxdb | `000009504`, `c45face6-2ef0-4c13-a266-7283a5e56ecd` | `dblogger` | — | listed |
| postgres | MONIT Metrics: batch/cloud/dcbynumbers/landb/netstat | `monit-metrics-<name>` | `metrics` | — | listed |

**Hiveeyes** (`https://weather.hiveeyes.org/grafana`) — environmental/IoT, ~50 InfluxDB
datasources (InfluxQL; some Flux). Proxy works: `dwd_cdc` (`000000004`) `SHOW
MEASUREMENTS` OK. Notable: `dwd_cdc` / `dwd_mosmix` (German weather service observations
and forecasts), `luftdaten_info` (`000000013`, citizen air quality), `uba_luft`
(`000000019`), `hiveeyes_*` (beehive sensors), `telegraf` (`000000017`),
`influxdb_internal` (`000000016`). Good for strong seasonality (daily/yearly), sensor
gaps, and physical units (°C, hPa, µg/m³).

**Grafana Play** (`https://play.grafana.org`)

| Type | Name | uid | Access |
|---|---|---|---|
| loki | grafanacloud-play-logs | `grafanacloud-logs` | `/loki/api/v1/labels` OK (k8s, app, cloud labels) |
| loki | LokiNGINXLogs | `ac4000ca-1959-45f5-aa45-2bd0898f7026` | listed |
| tempo | grafanacloud-play-traces | `grafanacloud-traces` | listed |
| elasticsearch | Elasticsearch: HTTP Logs (`play-logs-1`, `@timestamp`) | `play-elastic-http-logs` | `_mapping` 502 |
| influxdb | InfluxDB Cloud — InfluxQL / SQL; InfluxDB v2 + Flux (`_monitoring`) | `play-influx-cloud-influxql`, `play-influx-cloud-sql`, `play-influx-2-flux` | listed |
| graphite | grafanacloud-play-graphite | `grafanacloud-graphite` | listed |

**Wikimedia:** Loki "Public Logs" (`hG3h22g4k`) — `/loki/api/v1/labels` OK (labels:
channel, instance, source, status, user). Graphite (`000000001`,
`https://graphite.wikimedia.org` direct → 502 on `/metrics/find`; proxy untested).

**Percona PMM demo:** VictoriaLogs (`bejat95n0qcxsb`), ClickHouse (`PDEE91DDB90597936`),
Loki (`df30rccp6iwowd` → 400 "Invalid data source URL", not usable).

## Recommendations (all sources)

1. A **public source registry** in the repo (`deploy/public-sources.toml` or similar):
   name, base URL, flavor, backend, politeness settings, suggested use. Telemetry Nerd
   can connect to any of them by name; nothing is enabled by default.
2. **Fixtures** recorded per backend family (Prometheus, Thanos, Mimir, VM) from the
   small sources; Wikimedia only for discovery-scale fixtures.
3. **Order:** Grafana Play (microservices) and PromLabs/Prometheus demo (fixtures) →
   Wikimedia (scale, history) → Percona/VM playground (MetricsQL) → CERN (Mimir/Thanos,
   databases, storage) → local OTel demo only when scenarios need ground truth.
4. Hiveeyes, CERN Elasticsearch/OpenSearch, and the Loki sources are candidates for the
   later logs/InfluxDB/Elasticsearch adapters (spec §11.3).
