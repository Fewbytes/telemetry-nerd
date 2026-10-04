# Data Source Quirks

How each metrics backend handles missing, partial, extrapolated and downsampled data,
and what that means for `bucket_state` (see the missing-data design in
`docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md` §6). Each adapter
declares a `MissingDataSemantics` profile (`src/telemetry_nerd/sources/semantics.py`); this
doc is the evidence behind it.

**Status legend:** ✅ verified here, with fixture ids (relative to `tests/fixtures/missing-data/`)
and an offline test that pins it · 📖 documented upstream, link given, not reproduced ·
❓ unknown. A profile property is only `VERIFIED` (trusted by `bucket_state`) with a recorded
fixture; everything else is treated as "cannot tell".

Research bead: `telemetry-nerd-1h9.10`. Findings written 2026-10-02.

## How the evidence was gathered

- **Local lab** (`deploy/missing-data-lab/compose.yml`, `just lab-up`; driver
  `scripts/missing_data_lab.py`): Prometheus 3.x (default 5 m lookback), VictoriaMetrics
  v1.137 (defaults), plus two copies with tiny query limits / 1 d retention. Two kinds of
  ground truth: (1) **seeded history** written through remote write with exact sample
  timestamps, known gaps (1 to 20 samples long), a 10 min hole, one counter reset, an isolated
  sample and a histogram with one `le` series missing; (2) a **real scrape target** that, on a
  fixed timeline (`local/timeline.json`), answers 503 (3 min and 7 min), omits one series
  while up, resets a counter, and omits one `le` bucket; the same values are also pushed by
  remote write *without* staleness markers (the OTLP/remote-write shape). Fixtures:
  `prometheus/prom__*`, `victoriametrics/vm__*`, `*-limits__*`.
- **Public instances**, from the registry, 1 request at a time, ≥ 1.1 s apart, narrow queries
  (`scripts/record_missing_data.py public`): Wikimedia Thanos 0.38 (raw, 5m and 1h
  datasources), CERN EOS Thanos 0.32.5, Grafana Play and CERN OpenStack (Mimir), VictoriaMetrics
  playground (cluster) and Percona PMM (single node), the two Prometheus demos. Fixtures:
  `thanos/`, `mimir/`, `victoriametrics/<source>__*`, `prometheus/*-demo__*`.
- Tests: `tests/unit/test_missing_data_local.py`, `tests/unit/test_missing_data_public.py`,
  `tests/unit/test_semantics.py`. A backend upgrade that changes behaviour fails these only
  after re-recording; the recorders are the regression probe.
- Everything below was observed on: Prometheus 3.x (`latest` image), VictoriaMetrics v1.137.0
  single node, Thanos 0.38.0 / 0.32.5, Mimir r411 / 2.15.0, VictoriaMetrics 2.24 API cluster.

## Summary: surprises first

1. **The adapter's expression path fabricates samples.** For non-selector expressions
   (`sum(rate(...))`, `histogram_quantile(...)`) the adapter asks `count_over_time((expr)[step:res])`.
   A subquery is a series of instant evaluations, so lookback fills gaps: `count` says 4
   samples for a bucket inside a 9 min hole (Prometheus: filled for the whole 5 m lookback).
   `bucket_state` would call those buckets `ok`. Selector queries are honest. Fixed in
   `telemetry-nerd-1h9.11`: counts now come from the underlying selector where derivable,
   else the expression is flagged "cannot tell" (`sources/observed.py`; spec §6).
2. **VictoriaMetrics `increase()`/`delta()`/`idelta()` after a gap return the whole gap's change
   in the first bucket(s); `rate`/`irate` do not**. For `increase` (615 instead of 15, 41× the truth), and 0, not "no data", for the first steps
   inside a gap (VQ2; bead `telemetry-nerd-1h9.13`). Prometheus returns no value for those buckets.
3. **Pushed data (OTLP, remote write) gets no staleness markers**: a series that stopped
   keeps evaluating for the whole lookback: 285 s on Grafana Play's OTel-demo series, 300 s
   locally. Scraped series end within one scrape interval. Same metric, different
   truthfulness by ingestion path (PQ3, MQ1).
4. **VictoriaMetrics' fill window is about one series interval, not 5 m**, and depends on the
   detected interval (15 s series: ~22 s; 60 s series: ~66 s). A gap of three intervals
   already breaks the line (VQ1).
5. **`count_over_time` on a Thanos tier counts downsampled points** (12 per hour on the 5 m
   tier), not raw samples. Refutes the earlier belief (TQ2). The response never says which
   resolution answered; the tier is only chosen by `max_source_resolution`; the "downsample"
   datasources serve raw by default (TQ1).
6. **Prometheus `resets(x[step])` misses a reset on a step boundary** (window = step holds one
   sample); VictoriaMetrics sees it via the previous sample (PQ5).
7. **Limit errors are HTTP errors with backend-specific shapes, never truncated results**, but
   the adapter mapped them to a generic failure (Wikimedia answers `text/plain`). Fixed in this
   branch (PQ6).
8. **Retention edge is silent**: empty success before retention (Thanos, Mimir, Prometheus,
   VM reads); VictoriaMetrics silently drops *writes* older than retention (XQ3).
9. Thanos 0.32.5's lookback is **inclusive** (a gap of exactly 5 m is filled); Prometheus 3's
   is left-open. Thanos dedup hides replica gaps without any marker.

---

## Prometheus (and the PromQL engine shared by Thanos/Mimir)

### PQ1 Lookback fill, 3 min and 7 min target outages ✅

- A raw selector `query_range` fills a gap with the last sample for up to **300 s**
  (`query.lookback-delta`, readable from `/api/v1/status/flags`), left-open: a gap of exactly
  300 s is *not* filled (`prometheus/prom__gapfill_i60_raw`, `_samples`; i15 and i60 series
  with gaps of 1 to 20 samples).
- `*_over_time(x[step])`, `count_over_time` and friends never fill: windows without samples
  yield no point (`prom__count_w15`, `prom__count_selector_w60`).
- **But a killed scrape target does not get filled at all:** the failed scrape writes a
  staleness marker, so the series ends within one scrape interval, for the 3 min and the
  7 min outage alike (`prom__scrape_gauge_raw`, `prom__scrape_up`). Lookback fill only
  applies to gaps without markers (pushed data, PQ3).
- ⚠ **Subqueries fill like instant evaluation** (`prom__count_subquery_w60` vs
  `prom__count_selector_w60`): a `count_over_time` of the adapter's expression path
  (`(expr)[step:res]`) reports samples that do not exist. The adapter therefore takes `count`
  from the expression's selector (`count_over_time(sel[step])`, lifted through its
  aggregations) and drops filled values; binary arithmetic between derivable operands takes the per-bucket minimum of the operands'
  counts (`telemetry-nerd-mig`); expressions it cannot derive (filters, set operators, vector matching, offset) are "cannot tell" (`counts_are_observed`). Bead
  `telemetry-nerd-1h9.11`; tests `test_subquery_windows_fill_gaps_but_the_adapter_reports_only_observed_samples`,
  `test_fetch_values_keeps_only_values_of_buckets_that_observed_samples`.

### PQ2 Scrape failure vs vanished series ✅

Both write staleness markers; the series ends within one scrape (`prom__scrape_gauge_raw`,
`prom__scrape_vanish_raw`). Markers do not appear in `count_over_time` or Prometheus raw `[w]` samples
(`prom__scrape_gauge_samples`; see the VictoriaMetrics exception below). Telling them apart: `up == 0`
during a failed scrape; `up == 1` with the series absent when a series vanishes from a
successful scrape (`prom__scrape_up`). `up` could refine `empty` reasons; deferred.
**VictoriaMetrics does show the markers**: a raw range vector (`x[20m]`) contains NaN samples at
the start of each outage (`vm__scrape_gauge_samples`), Prometheus 3 hides them. Aggregations and
`query_range` never contain them. Consumers of raw samples (`scrape_interval()`) must skip NaN.

### PQ3 Remote write / OTLP ✅ (local); OTLP-ingested ✅ (Mimir, real world)

Pushed samples carry no markers: the series keeps evaluating for the whole lookback after the
sender stops. Locally (Prometheus): the 3 min gap is filled completely, the 7 min gap for 300 s
only (`prom__scrape_rw_gauge_raw`). An explicit stale NaN sent over remote write does end the series
at once (`prom__scrape_rw_stalemark_raw`). A real OTLP pipeline: Grafana Play `target_info` series
that stopped lingered 285 s (`mimir/grafana-play__play_otlp_ended_*`, MQ1). A local OTel
collector has not been run (`1h9.4`); remote write is the same ingestion shape.

### PQ4 `increase()` at the window edges ✅

- Window with < 2 samples: **no value** (`prom__increase_w15`, `rate_w15`), also on Thanos
  and Mimir (`thanos/wikimedia-raw__engine_increase_30s`, `mimir/grafana-play__engine_*`).
- ≥ 2 samples: extrapolated to the window edges (30 s window, two samples 15 s apart →
  exactly 30.0; `prom__increase_w30`), on Prometheus, Thanos 0.38 / 0.32.5 and Mimir
  (`*__engine_samples` / `engine_increase_2m`).
- Error vs truth (counter at exactly 1/s): 5% at a gap's edge (a 300 s window whose data ended
  15 s before its right edge reads 300 instead of 285; `prom__increase_w300`); 0% elsewhere on
  regular data. No post-gap spike: buckets inside and right after a gap are *absent*.
- **Decision:** the extrapolation distortion is bounded by the missing share of the window, so
  it is already captured by the sample-count rule (`observed < expected` → `partial`). No
  separate flag is needed; an `increase` bucket must carry the state of its `count` companion.

### PQ5 Counter resets ✅

`increase` and `rate` handle the reset inside a bucket (no negative bucket;
`prom__increase_w30`). `resets(x[step])` with window = step returns 0 for a reset that falls on
the boundary between two step windows (`prom__resets_w15`), and sees it with a wider window
(`prom__resets_w30`). Cost: one extra range query, same class as `count_over_time`. Raw-sample
detection is not available (the adapter never fetches raw samples). **Decision:** flag `reset` from
`resets(x[2*step])` -style windows or accept missed boundary resets and say so in the profile
(`reset_visible_in_step_window=False`); the value is still correct because `increase` is
reset-aware.

### PQ6 Errors vs warnings vs truncation ✅

HTTP errors, not truncation: more than 11,000 points per series → **400** `bad_data` (`exceeded
maximum resolution of 11,000 points per timeseries`; `prom__limit_points`); `--query.max-samples`
→ **422** `execution` (`query processing would load too many samples into memory`;
`prom-limits__limit_max_samples`); parse error → 400 `bad_data` (`prom__limit_parse`). No
`warnings` and no silently shortened results were seen. The adapter now maps limit messages to
`LimitExceeded` (`sources/semantics.py::classify_limit_error`).

## Thanos

### TQ1 Tier switching ✅

The querier serves the tier requested by the `max_source_resolution` query parameter; by default
it serves raw data, **also on the `thanos-downsample-5m/1h` datasources**
(`thanos/wikimedia-1h__*_count_over_time_default`: ~60 samples/h, 60 s spacing). Raw is kept
≥ 300 d on Wikimedia, so no automatic switch was observed. `max_source_resolution=auto` with a
1 h step picked the 5 m tier. **The response never says which resolution answered**: the envelope
is `{status, data}` only. Detect from sample spacing or from `count_over_time(x[w])` (w / tier
resolution). Where raw is gone and the tier answers automatically: unknown.

### TQ2 `count_over_time` on tiers ✅ (refutes the belief)

On the 5 m tier `count_over_time(x[1h])` = **12** at every step; on the 1 h tier 1 or 2
(`thanos/wikimedia-1h__wikimedia-1h_count_over_time_res5m`, `_res1h`, and `wikimedia-5m__`).
It counts downsampled points, not raw samples. The hourly max from a tier also differs from raw
at window edges (68.65 vs 63.19 in the first hour).

### TQ3 Partial responses in the HTTP API ❓

No `warnings` key in any recorded Thanos response; partial responses could not be provoked
on the public instances. 📖 `warnings` per
<https://thanos.io/tip/components/query.md/#partial-response>. **Adapter rule (1h9.12):** any
entry in `warnings` means data may be missing and makes the chunk's span UNKNOWN
(`PartialResponse: <message>`, data kept, chunk not cached as complete), except engine
annotations starting `PromQL info:` / `PromQL warning:`, which describe the expression and
become a `source_warning:<text>` caveat. An unrecognised message counts as partial (can't tell
=> unknown). Same rule for Mimir. Synthetic bodies only; not provoked live.

### TQ4 Deduplication ✅

`dedup=false` returns 2 series where the default returns 1
(`thanos/wikimedia-raw__dedup_on`, `dedup_off`): replicas are merged server side, so a gap
in one replica is filled from the other and is invisible. Nothing in the response says so.
`bucket_state` cannot know; this is a **source-level** caveat (`dedup_hides_replica_gaps`) for
the provenance footer, not a per-bucket flag.

### TQ5 `/metadata` non-determinism ✅ (non-determinism) / ❓ (cause)

Three identical `limit=5` calls returned three disjoint name sets
(`thanos/wikimedia-raw__metadata_limit_{0,1,2}`). Whether this is a per-call store subset
(partial response) and whether series lists in queries are affected: unknown; queries were
stable in every recorded exchange.

### Other Thanos findings ✅

- 0.32.5 (CERN EOS) lookback is inclusive: a one-sample series is still evaluated exactly
  300 s later (`thanos/cern-eos__eos_ended_raw`). Wikimedia 0.38: scraped pod series end ~55 s
  after the last sample, i.e. at the next scrape's stale marker (`wikimedia-raw__wm_ended_raw`).
- Limit error text/plain on Wikimedia's proxy, JSON on CERN's (`step_limit_over`).

## Mimir / Grafana Cloud

### MQ1 Series that ended ✅

Grafana Play `target_info` (OTLP-ingested, hosted OTel demo): the series is still evaluated
**285 s** after its last sample (no marker). `kube_pod_info` (scraped): ends 45 s after its last
sample, at the next 60 s scrape (`mimir/grafana-play__play_otlp_ended_*`,
`grafana-play_scrape_ended_*`). CERN OpenStack had no ended series in the time range.

### MQ2 Query-frontend split seams ✅

A 26 h range crossing UTC midnight at a 300 s step returned exactly the expected number of
points, no duplicates and none missing at the seam (`mimir/grafana-play__split_seam_up`).
One range, one backend; not exhaustive.

### MQ3 Limit errors ✅ (points) / 📖 (others)

`exceeded maximum resolution of 11,000 points per timeseries` → HTTP 400 `bad_data`
(`mimir/*__step_limit_over`). Series/chunk/length limits: patterns from the Mimir docs
(`err-mimir-max-series-per-query`, `err-mimir-max-query-length`), not reproduced.

## VictoriaMetrics

### VQ1 Gap-filling rule ✅

Raw selector fill window ≈ one detected series interval: a 15 s series fills ≤ ~22 s after its
last sample, a 60 s series ≤ ~66 s (`victoriametrics/vm__gapfill_i15_raw`, `_i60_raw`,
`_i60_step{10,30,60}`); a gap of 3 intervals already breaks the line. Not the 5 m of Prometheus.
`count_over_time` and `rollup` stay honest (absent in gaps; `vm__rollup_w60`). Windows are
left-open like Prometheus 3 (`vm__count_w15`). With a step wider than the interval the fill
window grows with the step (irregular; not modelled).

### VQ2 Post-gap `increase` ✅ (fake spike confirmed)

Counter +15 per 15 s sample, 10 min hole: `increase(x[15s])` at the first sample after the hole
returns **615** (41 samples' worth) instead of 15; also on 30 s, 60 s and 300 s windows
(`victoriametrics/vm__increase_w15`, `_w30`, `_w60`, `_w300`). Inside the hole VM returns **0** for
the first steps (not "no data"), then nothing. Every other bucket was exact (285 where
Prometheus extrapolates to 300; `vm__increase_w300`). **Decision:** VM buckets whose `count`
shows a preceding gap longer than the window are `source_filled` for `increase`/`rate` values
(superseded for `rate` by the table below).

**Other rollups (bead `telemetry-nerd-mj0`, VM v1.137.0).** Recorded by
`scripts/record_vm_post_gap.py` (`victoriametrics/vm__pg_<counter|gauge>_<func>_w<15|75>`; sample
every 15 s, 10 min hole, step 15 s, windows = step and 5 × step; counter +15 per sample, gauge a
+2 ramp). First sample after the hole, counter (gauge likewise, scaled):

| rollup | value at the first post-gap step (w15 / w75) | after | verdict |
|---|---|---|---|
| `increase`, `increase_pure` | 615 / 615 (truth 15 / 75) | 630, 645, 660, 675 on w75, then 75 | spike, `ceil(w/step)` buckets |
| `delta` | same as increase (gauge: 82 vs 2) | same | spike, `ceil(w/step)` buckets |
| `idelta` | 1215 / 1215: the raw sample value, as if nothing preceded (gauge: 260) | 15 at once, any window | spike, first bucket only |
| `rate`, `irate` | absent / absent | 1 (exact) from the next step | no spike: **not flagged** |
| `deriv` | 0 (single sample: no slope) / 0 | 1 | no spike |
| `rate_over_sum` | 81 / 16.2 = the window's own samples / window | continues | ignores the previous sample |

So only the rollups that return *change over the window* use the pre-gap sample as a baseline; the
rate family drops that bucket. The earlier flagging of `rate` ("averages across the gap") was wrong
and is removed. Nested calls: a subquery around a spiking call keeps the spike inside its window,
so reaches add (`max_over_time(increase(x[5m])[10m:1m])` spikes for 15 m).

Not verified: `rollup_delta`, `rollup_increase`, `rollup_rate`, `increase_prometheus` and other VM-only variants; they are not flagged.

**Blind spot.** If the query window starts inside a gap, the leading buckets read `absent` and the
first returned `increase`/`delta` is still computed from the sample before the window
(`vm__pg_straddle_increase_w15`: 615 first bucket). `bucket_state` cannot tell this from a series
that starts mid-window and a magnitude heuristic would flag healthy series, so it stays
undetected (documented in spec §6).

**Rolling counters as `increase` over `*_over_time` windows instead** (evaluated, not done): replacing
`increase(x[w])` with e.g. `last_over_time(x[w]) - first_over_time(x[w])` stays honest on VM (windows
have no previous-sample rule, absent in gaps) but loses the increment between windows, mishandles
resets and breaks Prometheus equivalence, and costs two queries; flagging is cheaper and keeps the
value users expect.

### VQ3 Partial-response marker ✅ (shape) / ❓ (provoked)

The cluster endpoint adds `isPartial` to every response (`false` observed;
`vm-playground__shape_range`); single node omits it (`percona-pmm__shape_range`). A node-down
case could not be provoked on the public cluster. 📖 `isPartial: true` and
`-search.denyPartialResponse`. **Adapter (1h9.12):** `isPartial: true` makes the chunk's span
UNKNOWN (`PartialResponse: ...`), data kept; covered by synthetic bodies.

### VQ4 Recent-data reliability ✅ (not reproduced) / ❓ (cache, push lag)

Neither backend hid or under-counted the newest bucket: a 60 s range query ending at "now"
(5 s step, `count_over_time(up[5s])`) had a point for the newest step, and re-querying 40 s
later gave identical counts, on Prometheus, VM with `nocache=1`, and VM with its cache
(`vm__recent_up`, `prom__recent_up`; default `-search.latencyOffset=30s`). That was a scraped
series; late-arriving pushed data and the response cache's `-search.cacheTimestampOffset` for
older ranges were not tested. `settle_ms` stays a cache-policy decision, not a source property.

## Cross-cutting

### XQ1 Expected samples per bucket ✅

Series in one query do not share an interval. Grafana Play, one 5 m window of `up`: counts 5, 10,
14, 15, 19, 20, 28... across series (push and scrape mix); VM playground: uniformly 20 (15 s)
(`mimir/grafana-play__xq1_samples_per_5m`, `victoriametrics/vm-playground__xq1_samples_per_5m`).
**Decision:** `expected` per series (median spacing per series or per job), not per source.
Current per-source `resolution_ms` is wrong for mixed sources.

### XQ2 Partial histogram bucket sets ✅

With one `le` series absent for 30 samples (seeded) and for 60 s (scraped) the other buckets
keep reporting; `sum by (le) (increase(...))` returns no point for the missing `le` and no
warning (`{prom,vm}__hist_increase`, `{prom,vm}__hist_scrape_increase`). Note
`increase(x[15s])` on 15 s-spaced histogram buckets is empty on Prometheus (one sample per window),
so the histogram path needs windows ≥ 2 × interval there. A scraped `le="1"` is normalised to
`"1.0"` by Prometheus, kept as `"1"` by remote write and VM (compare `le` as floats). The heatmap must
compare the `le` set per step and mark the column `partial`/`unknown`.

### XQ3 Retention edge ✅

Before retention every backend answers an **empty success**, never an error: Wikimedia Thanos,
Grafana Play, CERN OpenStack, Percona PMM, Prometheus demo at 900 d ago. CERN EOS and the VM
playground still hold data from 900 d ago, so retention is not universal. VictoriaMetrics
silently drops *writes* older than `-retentionPeriod` (204, counted in
`vm_rows_ignored_total{reason="small_timestamp"}`; `vm-limits__retention_write`). The earliest
timestamp is not cheaply queryable (Prometheus `headStats.minTime` is the head only). Flags
expose retention: Prometheus `/api/v1/status/flags` (`storage.tsdb.retention.time`,
`query.lookback-delta`), VM `/flags`.

### XQ4 Clock skew / out-of-order ✅ (writes)

Prometheus drops an out-of-order sample (the write returns 204 and the readback lacks it) and
answers 400 `duplicate sample for timestamp` for a different value at an existing timestamp
(`prom__ooo_write`). VictoriaMetrics stores both (including duplicate timestamps twice) and
reads sort them (`vm__ooo_write`). Neither marks it in query results.

---

## Profile summary (code: `sources/semantics.py`)

| property | Prometheus | Thanos | Mimir | VictoriaMetrics |
|---|---|---|---|---|
| gap fill (selector) | 300 s, left-open ✅ | 300 s, 0.32 inclusive ✅ | 300 s ✅ | adaptive, ~1 interval ✅ |
| `*_over_time` windows | left-open, absent when empty ✅ | 📖 | 📖 | ✅ |
| subquery fills gaps | ✅ | 📖 | 📖 | ✅ |
| markers on scrape failure | ✅ | ✅ | ✅ | ✅ |
| pushed data has markers | no ✅ | ❓ | no ✅ | no ✅ |
| rate/increase edge | extrapolate ✅ | extrapolate ✅ | extrapolate ✅ | previous sample ✅ |
| post-gap spike | no ✅ | 📖 | 📖 | **yes** ✅ |
| `resets(x[step])` sees boundary | no ✅ | 📖 | 📖 | yes ✅ |
| downsampled tiers | none | 5m, 1h ✅ via param | none 📖 | none (enterprise) 📖 |
| tier reported in response | n/a | no ✅ | n/a | n/a |
| partial response signal | `warnings` 📖 | `warnings` 📖 | ❓ | `isPartial` ✅ (cluster) |
| stale NaN in raw `x[w]` | hidden ✅ | 📖 | 📖 | **shown** ✅ |
| dedup hides replica gaps | n/a | yes ✅ | 📖 | ❓ |
| retention edge | empty ✅ | empty ✅ | empty ✅ | empty ✅ |
| out-of-order write | dropped ✅ | ❓ | dropped 📖 | accepted ✅ |
| limit errors | 400/422 ✅ | 400 ✅ | 400 ✅ (points) | 422 ✅ |

### Staleness markers are not observable through our query API

Reading a marker needs a raw range-vector probe (`x[w]` as an instant query), and only
VictoriaMetrics returns the stale NaN there; Prometheus hides it, Thanos/Mimir are documented-only
(table above). Our adapters read `query_range`, which carries no markers on any backend we verified or found documented, so no
adapter sets `Flag.STALE_MARKER` and fleet churn reports every stopped member as `silent`. A marker still ends a series within about one scrape interval in `query_range` (the series stops
rather than lingering for the lookback), so `silent` does not mean no marker was written. The
absence of a marker in our data is **not** evidence that none was written (principle 9: a marker
is a positive observation; its absence from a channel that cannot show it is unknown). Detecting
it would take a per-member raw probe around `last_seen`, VictoriaMetrics only (bead cr4 option);
not implemented: a backend-specific extra query per stopped member for one label of churn. The
consumer (`marked_stale`) is ready and pinned by tests should an adapter ever set the flag.

## Future sources (not MVP, for `sgb`)

Noted so the profile shape covers them:

- **Graphite:** explicit `None` per interval; consolidation honours `xFilesFactor` (the
  minimum fraction of non-null points). Maps directly onto `coverage`.
- **InfluxDB:** `fill(none|null|previous|linear|0)`. Must query with `fill(none)` or `null`.
- **Elasticsearch/OpenSearch date_histogram:** `min_doc_count: 0` gives empty buckets
  explicitly; missing docs ≠ zero docs only if ingestion health is known.
- **Datadog-style APIs:** default zero-fill for counts; interpolation on rollups.

## Reproducing

```bash
just lab-up
uv run --script scripts/missing_data_lab.py seed
uv run --script scripts/missing_data_lab.py serve      # ~21 min; writes local/timeline.json
uv run --script scripts/missing_data_lab.py probes     # write-side: out-of-order, retention
uv run scripts/record_missing_data.py local
just lab-down
uv run scripts/record_missing_data.py public           # polite; FORCE=1 to re-record
```
