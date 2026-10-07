# Design: Elasticsearch / OpenSearch source adapter

- **Date:** 2026-10-07
- **Status:** Approved design (brainstorm 2026-10-07); ready for an implementation plan
- **Bead:** details the Elasticsearch/OpenSearch part of `telemetry-nerd-sgb` (P4 placeholder,
  "Future adapters: logs and non-Prometheus sources"). InfluxDB, Loki and Graphite stay in
  `telemetry-nerd-sgb`; when this design is planned, the ES/OpenSearch part becomes its own epic.
- **Scope:** one new backend adapter (direct HTTP connection to an Elasticsearch or OpenSearch
  cluster), the config, discovery, distribution and Little's-law plumbing it needs, and the
  places where the service currently assumes every `expr` is PromQL. Nothing else.

# Framing

`Source.fetch(expr: str, rng, step_ms)` (`sources/base.py`) takes an opaque string. Every source
so far speaks PromQL (Prometheus, Thanos, Mimir, VictoriaMetrics), so `expr` has always *been*
PromQL by convention, never by type. This design keeps that contract and makes it explicit:

- **Each adapter interprets its backend's own native query language.** For Elasticsearch and
  OpenSearch, `expr` is literally an Elasticsearch Query DSL request body (a `query` clause,
  typically a Lucene `query_string` filter, plus an optional metric aggregation, optionally
  grouped by one `terms` level; see [The `expr` contract](#the-expr-contract)). It is never
  translated through an invented intermediate language, and never rewritten for display.
- **The panel shows the real query.** The panel's `question` (already required by `show()`) is
  the human-readable label. The raw native query is in the existing Query expander
  (`ui/src/Panel.svelte`: `<details class="query"><summary>Query</summary><pre><code>{data.dataset.expr}</code></pre>`),
  which already shows any string verbatim, on demand, with no length limit. No change to the
  expander is needed.
- **The pattern generalizes later.** Honeycomb, SQL, CloudWatch and Datadog adapters would each
  take their own native query text the same way. Building them is not part of this design; this
  epic proves the pattern end to end with one real adapter.

The project stays numeric-aggregate-first ("evidence-first telemetry workspace"): an ES source
produces the same `FetchResult` / `DistResult` every other source does, so the existing analysis
tools work on its datasets.

# Motivating use case

One Elasticsearch index of HTTP access-log documents (one document per request) gives two of
Little's law's three inputs with no extra instrumentation:

- **arrival rate** λ: the number of documents per query bucket (Elasticsearch's own
  `date_histogram` `doc_count`), as requests per second;
- **service time** W: the mean of a `response_time`-type field per query bucket (the `stats`
  sub-aggregation's `avg`, with `count` and `min`/`max`).

`check_littles_law(arrival_rate=..., latency=..., concurrency=...)` must accept native ES queries
for `arrival_rate` and `latency` directly. The third input, concurrency L, must be *measured*
(`NOT_POSSIBLE` in `core/littles_ops.py`: deriving L from λ·W would make the check circular). An
access log has no in-flight measurement, so L comes from another source, typically a Prometheus
in-flight gauge. That cross-source case is designed below in [Little's law](#littles-law-on-an-es-source).

# The `expr` contract

## Same split as PromQL

A PromQL `expr` contains no time-range literals: `start`/`end`/`step` are separate arguments, and
`PromQLSource._query_range` turns them into query parameters that never appear in the PromQL text.
Elasticsearch gets the identical split:

- `expr` = the native `query` (a filter) plus an optional aggregation: one metric aggregation,
  optionally inside one `terms` grouping. **No `date_histogram`, no time bounds.**
- The adapter builds the `date_histogram` from `rng`/`step_ms`, adds the time-range filter, and
  grafts the caller's aggregation tree under it, exactly as the Prometheus adapter adds
  `start`/`end`/`step` and its `*_over_time` rollups.

So the simplest query, request rate, needs no `aggs` at all: `date_histogram` buckets already
carry `doc_count`. This is a complete, valid "requests per second over time" query:

```json
{"query": {"query_string": {"query": "service:checkout AND NOT url.path:\"/healthz\""}}}
```

Latency (mean, min, max and count per query bucket):

```json
{
  "query": {"query_string": {"query": "service:checkout"}},
  "aggs": {"lat": {"stats": {"field": "response_time"}}}
}
```

What the adapter actually sends for the latency query, for a time range `S..E` at a 60 s query
step on a source with `time_field = "@timestamp"` (illustrative; the adapter's agg name is
reserved, see [Validation](#validation)):

```json
POST /<index_pattern>/_search
{
  "size": 0,
  "track_total_hits": false,
  "timeout": "30000ms",
  "query": {"bool": {"filter": [
    {"query_string": {"query": "service:checkout"}},
    {"range": {"@timestamp": {"gte": S_minus_60000, "lt": E, "format": "epoch_millis"}}}
  ]}},
  "aggs": {"__tn_time": {
    "date_histogram": {"field": "@timestamp", "fixed_interval": "60000ms",
                       "offset": "+<S mod 60000>ms", "min_doc_count": 0},
    "aggs": {"lat": {"stats": {"field": "response_time"}}}
  }}
}
```

The dataset's `expr` (and so the Query expander) holds the caller's string, unchanged. The
wrapper is not shown, just as `rollup(...)`, `start` and `step` are not shown for PromQL.

## Query forms

`expr` is a JSON object with at most the keys `query` and `aggs` (`aggregations` is accepted as
the ES synonym; not both). An absent `query` means all documents (`match_all`). The `aggs` shape
decides the **form**:

| Form | `aggs` | Per query bucket: avg = min = max | count | Representation |
|---|---|---|---|---|
| `rate` | none | `doc_count / step_s` (documents per second) | 1 | `bucket_agg` |
| `field_rate` | one `value_count` on a field | `value_count / step_s` | 1 | `bucket_agg` |
| `stats` | one `stats` on a numeric field | avg, min, max of the field (not equal) | `stats.count` | `bucket_agg` |
| `percentile` | one `percentiles` with exactly one entry in `percents` | the percentile value | documents that had the field (an adapter-added sibling `value_count`, `__tn_n`) | `quantile` |
| `histogram` | one `histogram` on a numeric field | see [Distributions](#distributions) | | `distribution` |

Each of the first four forms may be **grouped**: one `terms` aggregation whose only sub-aggregation
(if any) is the form's metric aggregation. A `terms` with no sub-aggregation is the grouped `rate`
form (each term bucket's `doc_count`). See [Grouping](#grouping).

Decisions in this table:

- **Rate forms are per second**, like PromQL `rate()`, so the value does not change with the
  query step and Little's law reads it as λ directly. The count column is 1 (one exact
  measurement per query bucket, as `fetch_values` does), not the document count: the value
  already is the count, scaled.
- **`stats` count is the number of documents that had the field**, the observations behind the
  mean, matching the glossary's **count** ("samples ... inside a query bucket": here a document is
  a sample). A bucket with `stats.count == 0` produces no row (no observation, no value).
- **`percentile` is a quantile representation**: per step, never rolled up or merged, routed
  through `fetch_values` exactly like a PromQL `histogram_quantile`. ES and OpenSearch percentiles
  are TDigest estimates; every such dataset carries the caveat `approximate_percentile` (TDigest,
  the request's compression, default 100). One percentile per query; several percentiles are
  several queries (each is a series of its own representation, the same as one
  `histogram_quantile(q, ...)` per expression).
- **`histogram` is only accepted by `query_distribution`** (`fetch_histogram`); `query()` refuses
  it with a hint naming `query_distribution`.
- **Other aggregations are refused in v1** (`avg`, `sum`, `min`, `max`, `cardinality`,
  `date_histogram`, `composite`, pipeline aggregations, anything with `script`): `stats` covers
  avg/min/max; the rest is [later work](#later-work).

## Query buckets and time

- A query bucket carries its **end time** (glossary: `(t − step, t]`). ES `date_histogram` keys
  are bucket *starts*, so the adapter emits `ts_ms = key + step_ms`.
- The range filter is `gte = rng.start_ms − step_ms`, `lt = rng.end_ms`, so the first query
  bucket ends at `rng.start_ms`, as in a Prometheus range query.
- `offset = rng.start_ms mod step_ms` aligns ES's interval grid to the requested one (the service
  already aligns ranges to the step, so it is usually `+0ms`; the adapter sets it anyway).
- **Edge closure differs:** an ES bucket is `[t − step, t)`, a Prometheus one `(t − step, t]`. A
  document stamped exactly on a boundary lands one bucket later than a sample would. This is
  stated in the adapter's docstring and the tool description; it is not corrected (it cannot be,
  without a second query).
- ES buckets are **tiles** by construction (glossary): each document is counted in exactly one
  query bucket, so counts add across buckets, and there is no query window or lookback.
- `min_doc_count: 0` without `extended_bounds`: ES then fills empty buckets *between* the first
  and the last non-empty bucket only. Interior empty buckets of the `rate`/`field_rate` forms are
  emitted as **0 per second** (a document count of zero is an exact observation of "no matching
  documents"); leading and trailing empty buckets produce no rows (the retention edge before the
  first document, and the future after the last, are unknown, never zero). The dataset carries
  the caveat `zero_is_no_documents`: ES cannot tell "no traffic" from "no ingestion" in an
  interior bucket.
- Late ingestion is not handled in v1: the newest buckets may still be filling. The adapter
  reports no settling signal of its own (see [Later work](#later-work)).

## Grouping

PromQL's `sum by (label) (...)` becomes one `terms` aggregation wrapping the metric aggregation:

```json
{
  "query": {"query_string": {"query": "service:checkout"}},
  "aggs": {"by_status": {
    "terms": {"field": "http.response.status_code", "size": 50},
    "aggs": {"lat": {"stats": {"field": "response_time"}}}
  }}
}
```

- Each distinct term becomes one series with `labels = {field: str(term)}` (`SERIES_SCHEMA`;
  `series_id(source, labels)` as for PromQL). Ungrouped forms are one series with `labels = {}`.
- **One grouping field in v1** (one `terms` level). Elasticsearch's `composite` aggregation would
  allow multi-field grouping; it is a later enhancement.
- The `terms` aggregation is the caller's and is sent as written. The adapter does not add
  `missing`: documents without the field are in no series unless the caller sets `missing`
  (stated in the tool description).
- **No silent truncation.** `terms` returns the top `size` terms *per query bucket* (the terms
  aggregation is nested under the adapter's `date_histogram`), so a too-small `size` would make
  series appear and vanish between buckets. If any query bucket reports `sum_other_doc_count > 0`,
  the fetch fails with `LimitExceeded` (hint: raise `size`, up to the series limit, or narrow the
  query). With `sum_other_doc_count == 0` in every bucket, every term is present and the counts are
  exact (each shard returned all its terms: the default `shard_size` exceeds `size`).
- In an interior query bucket, a group absent from the terms buckets had zero matching documents
  there (exact, by the rule above): for the grouped `rate`/`field_rate` forms it reads as 0 per
  second; for `stats`/`percentile` it produces no row.
- More than `Limits.max_series` (500) distinct series raises `LimitExceeded`, as for PromQL.

## Validation

The adapter parses `expr` before sending (`sources/esquery.py: EsQuery.parse(expr)`; pure, no I/O)
and refuses with `SourceError` and a hint listing the accepted forms when:

- `expr` is not a JSON object, or has keys other than `query` and `aggs`/`aggregations`;
- `aggs` has more than one entry at any level, or nests deeper than `terms` → metric;
- an aggregation type is outside the [forms table](#query-forms) or carries `script`;
- a metric aggregation has no `field`; `percentiles` has other than exactly one `percents` value;
  `terms` has no `field`;
- an aggregation name starts with `__tn_` (reserved for the adapter's own aggregations).

**Field checks.** Elasticsearch answers a metric or `terms` aggregation on an *unmapped* field
with empty results, not an error, which would read as "no data". So before sending, the adapter
checks every field the aggregations name (metric field, `terms` field, `histogram` field) against
`_field_caps` for the index pattern (one call per field, cached per source until the next
`discover`): unmapped → `SourceError` "field <f> is not in the mapping of <pattern>" (hint:
`source_learn` lists the fields); metric or histogram field not numeric → `SourceError`; `terms`
field not aggregatable → `SourceError` (hint: the `.keyword` sub-field).

The `query` clause is **not** parsed or validated beyond "is a JSON object": it is sent verbatim
and Elasticsearch validates it (errors map per [Errors](#errors)). A time constraint written into
`query` is honoured as a filter (the tool description tells Claude not to write one: time is
`start`/`end`). `EsQuery` also exposes what the service needs instead of a PromQL parse: the
form, the metric field, the grouping field, and the quantile `q` for the `percentile` form.

Equivalent spellings (whitespace, key order) are different strings and therefore different
dataset cache keys. The adapter does not canonicalize: the shown string is exactly what Claude
wrote.

# Distributions

Distribution support is in scope for v1, first-class: `query_distribution` and heatmap /
histogram / ECDF / CCDF / percentile-band panels work on an ES source.

## Request

`fetch_histogram(selector, by, rng, step_ms)`: `selector` is an `expr` whose `aggs` is exactly
one `histogram` aggregation (caller-chosen `field`, `interval`, optional `offset`, optional
`hard_bounds`; `keyed` and `script` are refused). `by` holds zero or one field name; the adapter
builds the `terms` level from it (as the PromQL adapter writes `sum by (le, ...)` from `by`), with
`size = Limits.max_series + 1`. The tree is:

```
date_histogram (time; adapter-built from rng/step_ms)
  └─ terms (series; adapter-built from `by`; optional, one level)
       └─ histogram (value buckets; the caller's field + interval)
            └─ doc_count of each leaf bucket = the count
```

Example `selector` (with `by=["service"]`):

```json
{
  "query": {"query_string": {"query": "NOT url.path:\"/healthz\""}},
  "aggs": {"lat": {"histogram": {"field": "response_time", "interval": 25}}}
}
```

A `terms` level inside `selector` is refused (grouping goes in `by`, as for PromQL), as is more
than one `by` field (multi-field grouping is later work). The `sum_other_doc_count` rule from
[Grouping](#grouping) applies.

## Mapping onto `DistResult`

| `DistResult` part | From the ES response |
|---|---|
| `rows` (`DIST_SCHEMA`) | one row per leaf bucket with `doc_count > 0`: `ts_ms` = time key + step, `series_id` from `{by_field: term}` (or `{}`), `bucket_lo` = value key, `bucket_hi` = value key + `interval`, `count` = `doc_count` (an exact integer, never extrapolated) |
| `columns` (`COLUMN_SCHEMA`) | one row per (series, query bucket) inside the first..last non-empty time bucket: `n` = sum of the leaf counts (documents that had the field; n may be 0) |
| `series` | as for `fetch` |
| `scheme` | `BucketScheme(kind="linear", width=interval, offset=offset or 0)` (new kind, below) |
| `expr` | the caller's `selector`, verbatim |
| `caveats` | `query_chosen_buckets` (always), `source_warning:*` / `failed` for partial responses as in [Errors](#errors) |

## Bucket edges: the real difference from Prometheus

Prometheus histograms have **source-defined** value buckets: classic `le` edges fixed by the
instrumentation, native exponential schemas chosen by the client, VictoriaMetrics' fixed
`vmrange` scheme. The bucket scheme is a property of the data and is the resolution limit of
every distribution read from it (`query_distribution` reports it as such).

Elasticsearch has no histogram metric for raw documents. **Every ES distribution's buckets are
chosen by the query** (`interval`, `offset`): the raw values are kept, so a finer interval can
always be asked for (at a cost in buckets). Consequences, all explicit:

- **New `SchemeKind` `"linear"`** in `model/distribution.py`, with two new optional
  `BucketScheme` fields `width` and `offset` (`edges` stays empty: the edge count is unbounded).
  `growth` is `None`. `to_dict`/`from_dict` carry `width`/`offset`; the UI's `BucketSchemeInfo`
  (`ui/src/lib/api.ts`) gains optional `width` and `offset`.
- **`describe()` text** for `linear`: `"fixed-width buckets of <width> (offset <offset>), chosen by
  the query; each [lo, hi)"`. For Prometheus kinds the text is unchanged. This is what
  `bucket.description` / `BucketSchemeInfo.description` shows for an ES distribution.
- **Panel note** (`ui/src/lib/panelNotes.ts`): the distribution note today says "from increase()
  of the histogram; bins are the source buckets". For a `linear` scheme it says "document counts
  per value bucket; bins chosen by the query (<description>)". The histogram-mark note changes
  the same way ("bars are the query's buckets"). The "resolution limit" wording in
  `query_distribution`'s summary becomes "the query's bucket width (re-query with a smaller
  interval for more detail)" for `linear`.
- **Edges are lower-inclusive.** An ES value bucket is `[lo, hi)`; a Prometheus `le` bucket is
  `(lo, hi]`. `analysis/fraction.py` counts buckets with `lo >= x` as above `x`, which on a
  `linear` scheme yields P(X ≥ x), not P(X > x). For integer-valued fields (milliseconds) the
  difference is material. `fraction_over` on a `linear` scheme therefore reports
  `"compare": ">="` and its text says "at or above x"; the CCDF panel note says "P(X ≥ x)" for
  `linear`. Prometheus kinds keep `>`. A `BucketScheme.lower_inclusive` property (`kind ==
  "linear"`) is the single place this is decided.
- Threshold snapping in `compare_seasonal` (to "the nearest shared edge") works unchanged: the
  edges are `offset + k·width`.
- `DIST_SCHEMA`'s `count` comment ("increase() over one step") becomes "observations in the value
  bucket over one query step (increase() for Prometheus, doc_count for Elasticsearch)".

Log-spaced or custom edges (the ES `range` aggregation, which would map onto the existing
`custom` kind) are later work.

# Little's law on an ES source

`check_littles_law` (`core/littles_ops.py`) today parses each role as a PromQL metric name or
selector (`_parse`) and writes the four queries itself (`_exprs`), all on one `source`. Changes:

- **New argument `concurrency_source: str | None = None`** (default: `source`). The concurrency
  role is fetched from it; arrival rate and latency from `source`.
- **When `source` is an ES source**, `arrival_rate` and `latency` are native ES queries, not names:
  - `arrival_rate`: the `rate` form (no `aggs`). It is already a per-second rate
    (`arrival_is_rate = True`, as for a gauge today).
  - `latency`: the `stats` form on the duration field. One fetch feeds both of the check's latency
    roles: `latency_sum = avg × count` and `latency_count = count` per query bucket (W is the
    mean of the documents, a mean as the check requires). A `percentile` form is refused with the
    existing "looks like a percentile" refusal.
  - Both use the same query step as the sub-step grid; ES buckets are tiles, so the check uses
    the tile path (`counter_form = "es_buckets"`, `lookback_ms = 0`, `tile_s = step_s`), not
    `rate(x[$__rate_interval])`.
  - `latency_unit`: from the field's catalog unit (mapping `meta.unit`, see
    [Discovery](#discovery-and-the-catalog)); otherwise the existing "assumed s, flagged" rule.
  - `binding` is refused (bindings come from the PromQL rule corpus; see [Non-goals](#non-goals)).
- **Concurrency is never read from an ES source in v1:** `concurrency_source` must be a PromQL
  source (an ES source there is refused with a hint). Documents that carry an in-flight
  measurement (periodic metrics documents) would need a gauge reading of a field, which no v1
  form defines.
- **Sub-step grid with two sources:** the grid uses the larger of the two sources' resolutions
  (`res`), and the gauge's series-interval probe (`_probe_scrape`) runs against
  `concurrency_source`.
- **Grouping across sources is refused in v1:** `by` must be empty when the roles span two
  sources (an ES group label `service.name` and a Prometheus label `service` are different names;
  joining them needs a label mapping, later work). Same-source checks keep today's behaviour.
- **Assumptions** gain one entry when sources differ: "arrivals and latency from <ES source>
  (documents, query buckets [t − step, t)), concurrency from <other source> (gauge read at the
  last scrape ≤ t)", status `assumed`.
- The tool description gains one paragraph with the ES example (arrival rate and latency as ES
  queries, `concurrency_source` naming the in-flight gauge's source).

# Connection and configuration

## `SourceSpec` (`sources/spec.py`)

- `flavor: Literal["prometheus", "victoriametrics", "elasticsearch", "opensearch"]`. OpenSearch's
  search and aggregation API is wire-compatible with Elasticsearch's (a fork): **one adapter
  serves both**. `flavor` only changes how `probe()` reads the version response.
- Two new flat fields, in the style of `profile_source` / `timezone`:
  - `index_pattern: str | None = None`, e.g. `access-logs-*`. Lowercase; may be a comma-separated
    list and use `*`; no whitespace or `/ \ " < > | #`; at most 255 characters. Bare `*` and
    `_all` are refused (they include system indices; hint: name the pattern). One source = one
    index pattern, so a field path names one catalog entry.
  - `time_field: str | None = None`, e.g. `@timestamp`. **No default.** The CERN indices in the
    public-test-sources spec use `@timestamp`, `timestamp`, `metadata.timestamp` and
    `metadata.event_timestamp`: any guess is wrong somewhere.
- Model validator: for `elasticsearch`/`opensearch` both fields are **required**; for the PromQL
  flavors both must be `None`. `profile_source` must be `None` for ES flavors in v1 (operating
  profiles are PromQL-only, see [Service integration](#service-integration)).
- `resolution_ms`: for ES flavors it is the finest query step the source accepts (events have no
  series interval). `None` means 1000 ms (assumed); a configured value overrides it. It is never
  learned.

## `AuthRef.scheme`

Add `"apikey"`: `Literal["bearer", "basic", "apikey"]`. `headers()` returns
`{"Authorization": "ApiKey <secret>"}`, the secret **verbatim**: it is the *encoded* API key
Elasticsearch returns on key creation (base64 of `id:api_key`), which Kibana and the
`_security/api_key` API both hand out ready to use. The adapter does not encode it (unlike
`basic`, which encodes `user:pass`). `bearer` stays valid for ES service-account tokens and
OpenSearch JWTs; `basic` for native users. The secret stays a reference (env var or file), as for
every scheme.

## `source_connect`

- `flavor` accepts `elasticsearch` and `opensearch`; new arguments `index_pattern` and
  `time_field` (passed into the spec; validated there). `url` is the cluster base URL (e.g.
  `https://es.example:9200`, or a reverse-proxy path prefix).
- `auth_scheme` accepts `apikey`.
- `grafana` + `uid` with an Elasticsearch datasource stays refused (Grafana-proxied ES is a
  non-goal). `UNSUPPORTED_HINT` in `sources/grafana.py` is reworded: Elasticsearch/OpenSearch
  connect directly with `source_connect(url=..., flavor="elasticsearch", index_pattern=...,
  time_field=...)`; through Grafana not yet. `SUPPORTED_TYPES` is unchanged.
- The registry factory (`core/bootstrap.py`, today `PromQLSource.from_spec`) becomes a dispatch
  on `spec.flavor`: `ElasticsearchSource.from_spec` for the ES flavors.

## The adapter (`sources/elasticsearch.py: ElasticsearchSource`)

Implements the `Source` protocol:

| Member | ES behaviour |
|---|---|
| `name`, `identity` | `identity = f"{flavor}|{url}|{index_pattern}|{time_field}|{resolution_ms}"` (the cache key must change when the index pattern or time field does) |
| `resolution_ms` | as above |
| `semantics` | `None` in v1 (no verified missing-data profile yet; the protocol allows `None`) |
| `query_language` | `"es_dsl"` (new protocol attribute, below) |
| `fetch` / `fetch_values` | one implementation; `fetch` refuses the `percentile` form (a percentile is never rolled up; the service routes it to `fetch_values`), `fetch_values` accepts every non-`histogram` form |
| `fetch_histogram` | [Distributions](#distributions) |
| `discover` | [Discovery](#discovery-and-the-catalog) |
| `probe` | below |
| `scrape_interval` | returns `None` (documents have no series interval) |

It reuses the existing transport conventions: one `httpx.AsyncClient`, `USER_AGENT`, the
politeness `Gate` (concurrency, min interval), `Limits(timeout_s=politeness.timeout_s)`, the
request body `timeout` set from it, and `MAX_STEPS_PER_QUERY` checked before sending
(`LimitExceeded`, same hint as PromQL).

**`probe()`**: (1) `GET /` → `version.number`, and for OpenSearch `version.distribution ==
"opensearch"`. A mismatch with `flavor` is reported in the probe result (`flavor_mismatch`), not
fatal (the APIs used are common to both). A 403 on `/` (no `monitor` privilege) is tolerated and
noted. (2) `GET /<index_pattern>/_field_caps?fields=<time_field>`: no matching index → `SourceError`
"index pattern matches no index"; time field absent or not `date`/`date_nanos` → `SourceError`
naming the date fields that do exist. Supported versions: Elasticsearch ≥ 7.10, OpenSearch ≥ 1.0
(an older version is reported as a probe error with the version found).

## `Source.query_language`

New protocol attribute: `query_language: Literal["promql", "es_dsl"]`. `PromQLSource` returns
`"promql"` for both its flavors (MetricsQL is a PromQL superset; `flavor` still distinguishes
them where it does today). The service and `DatasetMeta` use it instead of assuming PromQL
([Service integration](#service-integration)). Replay and fake sources return `"promql"`.

# Discovery and the catalog

`discover()` makes one call, `GET /<index_pattern>/_field_caps?fields=*&include_unmapped=false`,
and maps it onto the existing `Discovery` (`model/discovery.py`):

| `Discovery` field | From `_field_caps` |
|---|---|
| `metrics` | one `MetricInfo` per **numeric** field (`long`, `integer`, `short`, `byte`, `double`, `float`, `half_float`, `scaled_float`, `unsigned_long`) that is aggregatable, `name` = the field path. ES has no metric names: a numeric field under the source's index pattern fills that role. |
| `MetricInfo.type` | `time_series_metric` (ES 8 time-series data streams) or `meta.metric_type` when set (`gauge` / `counter`); otherwise `None` (unknown, not "untyped") |
| `MetricInfo.unit` | `meta.unit` when set: `ms`, `s`, `micros` → `us`, `nanos` → `ns`, `byte` → `B`; any other value kept verbatim |
| `MetricInfo.help` | `None` (mappings carry no help text) |
| `label_names` | aggregatable `keyword`, `constant_keyword`, `ip` and `boolean` fields (including the `.keyword` sub-fields of `text` fields): candidate `by` / `terms` fields. `text` fields are not aggregatable and are not listed. Numeric fields may also be grouped by (`terms` accepts them); they are listed under `metrics` only. |
| `histograms` | `{}` (the pre-aggregated ES `histogram` field type is skipped in v1, with a caveat) |
| `cardinality` | `None` (per-field cardinality would need one aggregation per field) |
| `metadata_coverage` | share of numeric fields with a `meta.unit` or a metric type |
| `caveats` | `cardinality_unavailable`; `mapping_conflict:<n>` (fields mapped with different types across the pattern's indices: excluded); `nested_fields_skipped:<n>` (fields under `nested` objects need `nested` aggregations: excluded); `histogram_fields_skipped:<n>`; `metrics_truncated:<limit>/<n>` (more than `Limits.max_metrics` numeric fields); `no_numeric_fields` when `metrics` is empty (not an error: the `rate` form needs no field) |
| `partial` | true when fields were excluded (`mapping_conflict`, `nested_fields_skipped`, `histogram_fields_skipped`) or metrics were truncated (`metrics_truncated`); `cardinality_unavailable` alone does not make it partial |

A discovered ES field becomes a `CatalogEntry` with `source` = the ES source and `metric` = the
field path; mapping-declared unit and type are `metadata`-origin claims, as source metadata is
for Prometheus. Everything else in the catalog (user/claude claims, stats, thresholds, typical
range) works unchanged on these entries.

**Name-based inference is skipped for ES sources.** `catalog_learn`
(`core/workspace_service.py`) applies name-template family detection (`detect_families`), T0
naming rules (`derive_claims`: `_total` → counter, `_seconds` → s, ...) and knowledge packs
(`otel_semconv`, `node_exporter`, `kubernetes`). All three encode Prometheus naming conventions
and would misread field paths (a document field named `requests_total` is not a counter).
`Discovery` gains `naming: Literal["prometheus", "fields"] = "prometheus"`; the ES adapter sets
`"fields"`, and `catalog_learn` then writes the declared metadata claims only.

# Service integration

`service.query` (`core/service.py`) dispatches on `src.query_language`. `promql` keeps today's
pipeline. `es_dsl`:

1. `EsQuery.parse(expr)` (validation errors → `SourceError`);
2. skips name-template family expansion, `$__rate_interval` expansion, PromQL `analyze`, catalog
   mergeability checks (`nonmergeable_uses`) and `settle_unobserved` (ES buckets are exact tiles:
   there is no lookback fill to settle);
3. representation from the form: `percentile` → `quantile` (with `q` from `EsQuery`, `n` from the
   count column the adapter fills from its `__tn_n` value count: no separate count query), `histogram` → refused with a hint naming
   `query_distribution`, everything else → `bucket_agg`;
4. fetches through the same series cache (`src.identity`, `expr`, range, step).

`DatasetMeta` records `query_language` (default `"promql"` for existing rows), so `show`, panel
notes and analysis never re-parse an ES `expr` as PromQL, even after the source is disconnected.

| Operation on an ES dataset or source | v1 behaviour |
|---|---|
| `show`, `analyze` (window baseline), `spectrum`, `fleet`, `filter`, `fraction_over`, heatmap / histogram / ECDF / CCDF / percentile panels | work unchanged (they read datasets) |
| `analyze` (reference baselines), `compare_seasonal` (time series and distributions) | work: they re-fetch the same `expr` over other time ranges through `fetch` / `fetch_histogram` |
| `check_littles_law` | as designed in [Little's law](#littles-law-on-an-es-source) |
| `show` auto-forms that read PromQL (counter drawn as its rate, unit and bounds derivation rules) | skipped; unit comes from the field's catalog claim for `stats` / `percentile` / `histogram`, and is unset for the rate forms (a document is not necessarily a request: Claude passes `unit`, e.g. `req/s`, when it knows) |
| `operating_profile`, normal band overlay | refused with a hint (profiles build PromQL rollups) |
| `catalog_bind` suggestions, `catalog_relations` from packs, RED/USE/Little's-law auto-detection | none produced (see [Non-goals](#non-goals)); manual catalog claims work |
| `source_learn` | runs `discover` + `catalog_learn`; no resolution learning |

Tool descriptions updated: `query` (a second paragraph for ES sources with the rate and latency
examples and the forms), `query_distribution` (the ES `selector` form, `by` ≤ 1 field,
lower-inclusive edges), `source_connect` (flavors, `index_pattern`, `time_field`, `apikey`),
`check_littles_law` (`concurrency_source`, the ES example), `source_discover_grafana` (the new
unsupported reason text).

# Errors

All failures are the existing typed errors from `sources/base.py`, each with a `hint`:

| ES failure | Signal | Raised as |
|---|---|---|
| Connection refused, DNS, TLS | `httpx.HTTPError` | `SourceUnavailable` "cannot reach <url>" (check url, cluster up) |
| Client-side timeout | `httpx.TimeoutException` | `SourceUnavailable` "query timed out after Ns" (narrow query, shorten range, coarser step) |
| Server-side timeout / shard failures | 200 with `timed_out: true` or `_shards.failed > 0` | data kept; the chunk's span in `FetchResult.failed` / `DistResult.failed` as `"PartialResponse: <reason>"`, as for a partial PromQL response |
| Auth failure | 401 | `SourceError` "authentication failed" (check the secret reference and `auth_scheme`: `apikey` for API keys) |
| Missing privilege | 403 `security_exception` | `SourceError` with ES's reason (needs `read` and `view_index_metadata` on the index pattern) |
| Index not found | 404 `index_not_found_exception`, or `_shards.total == 0` (a wildcard matching nothing answers 200) | `SourceError` "index pattern matches no index" (check `index_pattern`) |
| Mapping has no numeric fields | discovery | not an error: `Discovery` with no metrics and the `no_numeric_fields` caveat (the `rate` form still works) |
| Aggregated field unmapped / wrong type | adapter field check (ES itself would answer empty) | `SourceError` naming the field (see [Validation](#validation)) |
| Bad query / bad field | 400 `parsing_exception`, `x_content_parse_exception`, `search_phase_execution_exception` with `query_shard_exception` | `SourceError` with ES's root-cause reason (check Query DSL / Lucene syntax and field names; `source_learn` lists the fields) |
| Aggregating a `text` field | 400 `illegal_argument_exception` (fielddata) | `SourceError` (use the `.keyword` sub-field) |
| Unsupported `expr` shape | adapter validation | `SourceError` listing the accepted forms |
| Too many buckets | `too_many_buckets_exception` (`search.max_buckets`, default 65,536 on ES, 65,535 on OpenSearch) | `LimitExceeded` (coarser step, shorter range, larger histogram `interval`, fewer groups) |
| Truncated groups | `sum_other_doc_count > 0` in a query bucket | `LimitExceeded` (raise `terms.size` or narrow) |
| Query too large for memory | 429 `circuit_breaking_exception` | `LimitExceeded` (narrow the query) |
| Cluster overloaded | 429 `es_rejected_execution_exception`, any 5xx | `SourceUnavailable` (retry shortly) |
| Not an ES endpoint | non-JSON body | `SourceUnavailable` (url points at a proxy or login page) |
| Time field missing / not a date | `probe()` | `SourceError` naming the date fields found |

ES error bodies are `{"error": {"type", "reason", "root_cause": [...]}, "status"}`; the adapter
reports the first root cause's `type: reason`, never the whole body.

# Testing

Follows the project's test tiers (`CLAUDE.md`): unit tests and e2e never touch a real backend;
`tests/integration` uses containers; `-m network` reaches public endpoints and is non-blocking.

- **Unit** (`tests/unit`): `EsQuery.parse` (every form, every refusal); request building
  (range bounds, `offset`, reserved names, the grafted tree); response mapping for each form,
  grouped and ungrouped, with recorded/hand-written ES response JSON under
  `tests/fixtures/elasticsearch/` served through `httpx.MockTransport`; interior zero-fill and
  leading/trailing gaps; `sum_other_doc_count` refusal; `DistResult` mapping and the `linear`
  scheme (`describe`, `to_dict`/`from_dict`, `lower_inclusive`, `fraction_over` `>=`); every row of
  the [Errors](#errors) table; `SourceSpec`/`AuthRef` validation (`apikey` header); `discover`
  mapping including conflicts, nested and histogram fields; `catalog_learn` with
  `naming="fields"` writing no rule or pack claims; Little's law with an ES source and a
  PromQL concurrency source (fixture datasets, cross-source `by` refusal).
- **Integration** (`-m integration`, CI-required, testcontainers like the VictoriaMetrics
  tests): one Elasticsearch 8.x and one OpenSearch 2.x container, a small access-log index
  written by the test (known per-bucket counts and latencies), checked end to end through the
  service: each form, grouping, `query_distribution`, `probe` on both flavors, `discover`.
- **Live** (`-m network`, non-blocking): the CERN Elasticsearch/OpenSearch indices catalogued in
  `2026-10-01-public-test-sources.md` (e.g. `esnet` `esnet_*` with `timestamp`;
  `monit_es_fts_agg` `monit_prod_fts_agg*` with `metadata.timestamp`; the OpenSearch 3.4
  `monit_os_wlcgops_*` indices), the way that spec validated against live public endpoints.
  They are only reachable through CERN's Grafana datasource proxy, connected here as a plain
  `url` (`https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/<uid>`). The spike
  verified only `_mapping` through that proxy; whether it forwards `_search` and `_field_caps` is
  unknown. **The first live step is a probe of those two paths** (one request each, politeness as
  for CERN in the public-test-sources spec). If the proxy refuses them, live validation of this
  epic is the integration containers only, and CERN moves to the Grafana-proxied follow-up (the
  adapter is not changed to suit Grafana's proxy in v1).
- **UI** (vitest): panel-note text for `linear` schemes and the `>=` CCDF wording. No new e2e
  spec: the UI changes are text only, and the Query expander already renders any string.

# Non-goals

Each is deliberate; follow-ups are listed in [Later work](#later-work).

- **No RED/USE/Little's-law auto-detection (`binding_suggest`) for ES sources.** The whole
  suggestion corpus (`catalog/binding_suggest.py`, `catalog/relations.py`) is hardcoded PromQL
  templates keyed to standardized exporter naming (node_exporter, OTel semconv). Bespoke ES field
  names have no analog. Elastic Common Schema (ECS) field names (`http.response.status_code`,
  `event.duration`, `service.name`) *are* semi-standardized and would make suggestions viable on
  ECS-conformant indices: that is a follow-up, not designed here.
- **No Kibana path.** No query or proxy through Kibana. A Kibana *discovery* front door is a
  follow-up, not designed here.
- **No Grafana-proxied ES access.** Direct connection only. Extending `SUPPORTED_TYPES` in
  `sources/grafana.py` to `elasticsearch` with the proxy pattern used for Prometheus-compatible
  backends is separable work.
- **No multi-field grouping.** One `terms` level; the `composite` aggregation is the later route.
- **No full-text search or document browsing.** Lucene / Query DSL filters are fully supported
  as the `query` clause (scoping what is aggregated), but "show me the matching documents" and
  log-line browsing are not a new panel kind or query primitive. The project stays
  numeric-aggregate-first.
- **No other native-language adapters** (Honeycomb, SQL, CloudWatch, Datadog): the pattern is
  meant for them; building them is not this epic.

# Later work

Beads to file when this design is planned (names, not yet created):

1. **ECS catalog pack**: an `ecs` knowledge pack mirroring `catalog/packs/otel_semconv.toml`
   (units, types, roles for ECS fields), plus ES-form rule templates so `binding_suggest` can
   propose RED and Little's-law bindings on ECS-conformant indices.
2. **Kibana discovery front door**: read Kibana's saved-objects API (index patterns / data views,
   saved searches) to bootstrap `source_connect` (index pattern, time field), parallel to
   `source_discover_grafana` / `sources/grafana.py`. Discovery only; queries still go to ES.
3. **Grafana-proxied Elasticsearch/OpenSearch**: `SUPPORTED_TYPES` + `source_connect(grafana,
   uid)` for ES datasources (index and time field from the datasource's `jsonData`); unlocks the
   CERN sources if the plain-url probe above fails.
4. **Multi-field grouping** via the `composite` aggregation.
5. **More aggregation forms**: `sum` (e.g. bytes per second), `range` buckets for log-spaced /
   custom value edges (maps onto the `custom` scheme kind), `runtime_mappings`.
6. **ES missing-data semantics profile** (`sources/semantics.py`), with verified evidence like the
   PromQL profiles: retention edge, late ingestion / settling (an ingest-lag setting), partial
   shard behaviour.
7. **Operating profiles on ES sources** (profiles build PromQL rollups today).
8. **Grouped cross-source Little's law**: a label mapping between an ES group field and a
   Prometheus label; and a gauge form for reading concurrency from ES documents that carry an
   in-flight field.
9. **AWS SigV4 auth** for Amazon OpenSearch Service.
10. **Pre-aggregated ES `histogram` field type** as a source-defined distribution (it would carry
    source-defined edges, unlike the query-defined buckets here).

The remaining `telemetry-nerd-sgb` scope (InfluxDB, Loki, Graphite, other log stores) is unchanged
by this design.

# Files touched (for the plan)

| Area | Files |
|---|---|
| Config | `sources/spec.py` (flavors, `index_pattern`, `time_field`, validator, `apikey`) |
| Adapter | new `sources/elasticsearch.py`, new `sources/esquery.py`; `sources/base.py` (`query_language`) |
| Wiring | `core/bootstrap.py` (factory dispatch), `mcp/server.py` (`source_connect`, `check_littles_law`, tool descriptions), `sources/grafana.py` (`UNSUPPORTED_HINT`) |
| Service | `core/service.py` (`query` dispatch, `DatasetMeta.query_language`), `core/littles_ops.py` (ES roles, `concurrency_source`), `core/workspace_service.py` (`catalog_learn` naming) |
| Models | `model/distribution.py` (`linear` kind, `width`, `offset`, `lower_inclusive`), `model/discovery.py` (`naming`), `analysis/fraction.py` (`>=` on `linear`) |
| UI | `ui/src/lib/api.ts` (`BucketSchemeInfo`), `ui/src/lib/panelNotes.ts` (distribution / histogram / CCDF notes) |
| Tests | `tests/unit/...`, `tests/fixtures/elasticsearch/`, `tests/integration/...` (ES + OpenSearch containers), a `-m network` CERN probe |
