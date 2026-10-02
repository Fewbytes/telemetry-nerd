# Data Source Quirks

How each metrics backend handles missing, partial, extrapolated and downsampled data,
and what that means for `bucket_state` (see the missing-data design in
`docs/superpowers/specs/`). Each adapter declares a `MissingDataSemantics` profile. This doc
is the evidence behind each profile.

**Status legend:** ✅ verified here (with a fixture or probe reference) · 📖 documented
upstream, link given, not yet reproduced · ❓ belief, unverified. Nothing gets into an
adapter profile as ✅ without a recorded fixture.

Research bead: `telemetry-nerd-1h9.10`.

---

## Why this matters

`bucket_state` must never say `ok` for a bucket the source filled in, extrapolated
or silently dropped. Sources differ in exactly the places we care about:

- **gap filling**: does the source invent values across gaps?
- **staleness**: can we tell "series ended" from "scrape missed"?
- **edge handling**: how `rate`/`increase` treat partial windows and resets.
- **resolution**: raw vs downsampled tiers, and when the tier switches.
- **partial responses**: some stores down, answer still returned.
- **retention**: where `unknown` starts.

## Probe targets

From `docs/superpowers/specs/2026-10-01-public-test-sources.md`:

| backend | instance | notes |
|---|---|---|
| Thanos 0.38 | Wikimedia `000000026`, `thanos-downsample-5m`, `thanos-downsample-1h` | raw + both downsampled tiers; ≥1y retention |
| Thanos 0.32.5 | CERN EOS | older Thanos |
| Mimir (Grafana Cloud) | Grafana Play `grafanacloud-prom` | OTel demo; pod churn likely |
| Mimir 2.15 | CERN OpenStack, CERN DBoD | |
| VictoriaMetrics (single) | Percona PMM demo | MetricsQL `rollup()` |
| VictoriaMetrics cluster | play.victoriametrics.com | best metadata |
| Prometheus (vanilla) | none public; local OTel demo stack (`1h9.4`) | controlled faults via flagd (`1h9.5`) |

The local stack is the only place we control the ground truth (kill a target, pause a
scrape, reset a counter). Public instances give real-world shapes but no ground truth.

---

## Prometheus (and the PromQL engine shared by Thanos/Mimir)

Current beliefs:

- 📖 Instant/range-query evaluation uses a **lookback delta** (default 5m): the value at `t` is
  the latest sample in `(t − 5m, t]`. A gap shorter than lookback is filled in.
  <https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness>
- 📖 **Staleness markers:** when a target scrape fails, or a series disappears from a
  successful scrape, Prometheus writes a stale NaN. Evaluation stops returning the series
  immediately. So scrape failures make real gaps, while other gaps (remote-write lag,
  federation, out-of-order) are filled up to lookback.
- 📖 `rate`/`increase` **extrapolate** to the window edges, up to ~1.1× the average sample
  interval, and need ≥2 samples in the window. They handle counter resets internally.
  `resets()` counts them.
- 📖 `*_over_time(x[w])` uses only samples inside the window. No lookback filling. Window is
  left-open: `(t − w, t]`.
- 📖 `up{job,instance}` = 1/0 per scrape. 0 = target down; series of that target are stale.
- 📖 API responses can carry `warnings` (e.g. partial data).
- ❓ Remote-written data (OTLP / remote-write receiver) has **no staleness markers** unless the
  sender writes them, so gaps are filled up to lookback. Impact on OTel-demo data via VM/Prom.

Research questions:

1. **PQ1** Reproduce lookback fill: kill a target for 3m and for 7m. What does `query_range`
   with a raw selector return vs `count_over_time[step]`? (local stack)
2. **PQ2** Scrape-failure vs series-vanish: both write stale markers? Is the series visible
   in `count_over_time` after the marker? Can we tell them apart from `up`?
3. **PQ3** Remote-write / OTLP ingestion: are staleness markers present? When an OTel
   collector stops sending, how long does the series persist in evaluation?
4. **PQ4** `increase()` on a bucket with 1 sample → no value; with 2 samples near the edges →
   extrapolated. Quantify the error vs the true count for our step sizes; decide when to
   flag `source_filled` / `partial`.
5. **PQ5** Counter reset inside a bucket: `increase` vs `resets` vs our own detection from
   raw samples. Which is cheap enough per bucket?
6. **PQ6** Which errors come back as HTTP errors vs `warnings` vs silently truncated results
   (e.g. `query.max-samples`)?

## Thanos

- 📖 Downsampled tiers: 5m after 40h, 1h after 10d (compactor defaults). Querier picks the
  resolution via `max_source_resolution` / auto-downsampling.
  <https://thanos.io/tip/components/compact.md/#downsampling>
- 📖 Downsampled blocks store aggregates (count, sum, min, max, counter) per window. `*_over_time`
  over downsampled data is computed from these aggregates.
- 📖 **Partial response:** if a store is down, the querier may return partial results with a
  warning (`partial_response` strategy).
- 📖 **Deduplication** of HA replicas (`replica` label): gaps in one replica are filled from
  the other (penalty-based).
- ❓ `count_over_time` on downsampled data returns the *raw* sample count (from the count
  aggregate), not the number of downsampled points.

Research questions:

7. **TQ1** At what range does Wikimedia's querier switch tiers, and does the response say
   which resolution was used? If not, how do we detect `interval_change`?
8. **TQ2** `count_over_time` on `thanos-downsample-5m`: raw count or aggregate-point count?
   (Compare the same window against raw `000000026`.)
9. **TQ3** Do partial responses show up as `warnings` in the HTTP API through the Grafana
   proxy? Can we provoke or find one?
10. **TQ4** Does dedup hide replica gaps? Does `bucket_state` need a `deduplicated` flag?
11. **TQ5** `/metadata` non-determinism (88k vs 48k, already seen): same cause as partial
    response? Does it affect series lists in queries too?

## Mimir / Grafana Cloud

- ❓ Same PromQL engine and lookback as Prometheus; OTLP ingestion common (no staleness markers).
- ❓ Query-frontend splits and caches by time; results-cache can return stale data for recent
  ranges.
- ❓ Limits (`max_fetched_series`, `max_samples`) produce errors, not truncation.

Research questions:

12. **MQ1** OTel demo on Grafana Play: find a pod that churned (series ended). How long after
    its last sample does evaluation keep returning it?
13. **MQ2** Do query-frontend split boundaries ever create seams (duplicate or missing buckets)
    at split edges for our step alignment?
14. **MQ3** Limit errors: exact messages, so the adapter maps them to `unknown` with a reason.

## VictoriaMetrics

- 📖 No fixed lookback. Gap handling depends on the detected scrape interval; there are
  `-search.minStalenessInterval` / `-search.maxStalenessInterval` flags.
  <https://docs.victoriametrics.com/keyconcepts/#query-data>
- 📖 `rate`/`increase` **do not extrapolate**. They use the last sample *before* the window
  as the start value, so `increase` is exact for integer counters. Differs from Prometheus.
  <https://docs.victoriametrics.com/metricsql/>
- 📖 `rollup()` returns min/max/avg in one call (already used by the adapter).
- ❓ Staleness markers supported when written by vmagent / Prometheus remote-write. Not
  written for OTLP pushes.
- ❓ `-search.cacheTimestampOffset` / response cache: very recent buckets may be cached
  incomplete.
- ❓ Downsampling (enterprise only) changes resolution with age, like Thanos.

Research questions:

15. **VQ1** Exact gap-filling rule: at what gap length (as a multiple of the scrape interval)
    does a raw-selector `query_range` break the line? Does `count_over_time` stay honest?
16. **VQ2** `increase` with the "previous sample" rule: when the previous sample is far
    before the window (after a gap), is the increase attributed to the first bucket after
    the gap? That would be a fake spike right after missing data.
17. **VQ3** Does the playground cluster return partial-response markers (`isPartial`) when a
    vmstorage node is unavailable?
18. **VQ4** Recent-data cache: how far back from `now` are buckets unreliable? (Relates to
    the cache's `settle_ms`.)

## Cross-cutting questions

19. **XQ1** Expected samples per bucket: is the median inter-sample gap (current
    `resolution_ms` probe) good enough per series, or do series in one query have different
    intervals (OTel push vs scrape)?
20. **XQ2** Histogram buckets (`le`/`vmrange`/native): does missing data in one bucket series
    but not others happen (partial scrape)? How should it show in heatmap `bucket_state`?
21. **XQ3** Retention edge: what does each backend return before retention: empty or error?
    Can we query the earliest timestamp cheaply?
22. **XQ4** Clock skew / out-of-order samples: any backend that drops or reorders them, and
    is it visible?

## Future sources (not MVP, for `sgb`)

Noted so the profile shape covers them:

- **Graphite:** explicit `None` per interval; consolidation honours `xFilesFactor` (the
  minimum fraction of non-null points). Maps directly onto `coverage`.
- **InfluxDB:** `fill(none|null|previous|linear|0)`. Must query with `fill(none)` or `null`.
- **Elasticsearch/OpenSearch date_histogram:** `min_doc_count: 0` gives empty buckets
  explicitly; missing docs ≠ zero docs only if ingestion health is known.
- **Datadog-style APIs:** default zero-fill for counts; interpolation on rollups.

## Method

Per backend:

1. **Probe script** (`scripts/probe_source_semantics.py`, `uv run`): runs the canonical
   queries (raw selector `query_range`, `count_over_time`, `rollup`/`*_over_time`,
   `increase`, `resets`, `up`) over a window with a known event. Records request and
   response as fixtures (feeds `1h9.6`).
2. **Ground truth** from the local stack (`1h9.4` + `1h9.5` scenarios): kill target, pause
   scrape, reset counter, stop OTLP sender. Each scenario has a ground-truth timeline.
3. **Answer** each question in this doc: change ❓/📖 to ✅ with fixture name, or record the
   refutation.
4. **Encode** the answers as the adapter's `MissingDataSemantics` plus fixture tests that pin
   them, so a backend upgrade that changes behaviour fails CI.
