# Knowledge packs (2as.5): node_exporter and Kubernetes

Curated, versioned facts about well-known exporters, applied as `pack` claims during
`catalog_learn` (after name rules and declared metadata). Pack claims outrank both, never Claude or
the user. Confidence 0.85, citation `pack <name>@<version>: <url>`.

## Format
TOML in `catalog/packs/*.toml`: a `[pack]` header (name, version, citation) and `[[metric]]` entries
selecting by `name`, `names = [...]` or a full-match `match` regex, asserting any of type, unit,
bounds, additivity_series/time, role, description, bounded_by. Linted on load: canonical units,
role vocabulary (`ROLES`), valid catalog values, no duplicate exact names, valid regex. Exact
entries beat regex entries on the same field.

## `bounded_by`
(Superseded by 2as.8: now a relation kind, not a field; packs emit relation claims. Original text:)
New catalog field: the metric never exceeds the target at the same label set (avail <= size,
MemAvailable <= MemTotal, replicas_available <= status_replicas). Usage-vs-limit relations that
need label matching are 2as.8.

## Grounding
The packs were written against live `/api/v1/metadata` (Wikimedia Thanos for node_exporter,
Grafana Play for kube-state-metrics/cAdvisor). Offline slices live in `tests/fixtures/packs`;
tests require every exact name, regex and `bounded_by` target to exist in them and no pack type
to contradict a declared one. `scripts/refresh_pack_fixtures.py` re-fetches and reports drift.

Findings the real data forced: node_exporter exports `node_vmstat_*`/`node_netstat_*` as `unknown`
though cumulative (pack supplies counter); `node_network_speed_bytes` is bytes per second; cAdvisor
memory gauges lack `_bytes` (`container_memory_rss`); `container_fs_inodes_total` is a gauge;
timestamps like `node_boot_time_seconds` are not durations.

## Other changes
Rules gained Prometheus base-unit suffixes (`_hertz`, `_watts`, `_joules`, `_volts`, `_amperes`,
`_celsius`). `CatalogEntry.conflicts()` ignores descriptions (differing prose is not a
contradiction).

## Out of scope
OTel semconv pack (follow-up bead), kube-scheduler/apiserver/kubelet internals, label-aware relations.
