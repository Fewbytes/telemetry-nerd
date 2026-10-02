# Series Bundles, Localized Caveats, Missing Data — Design

Status: approved in brainstorm 2026-10-01/02. Next: implementation plan.

Related: `docs/telemetry-graphing-guide.md` (§3, §5 Hartmann/SfE), `docs/data-source-quirks.md`,
spike `telemetry-nerd-1h9.10` (per-source missing-data semantics), MVP spec §3.1–3.2, §6.

## 1. Problem

"No info" is information. Today a missing bucket is a null that breaks a line (easy to miss; an
isolated point vanishes), heatmaps draw "no data" and "measured zero" identically, a failed fetch
chunk fails the whole query, and sources silently fill gaps (Prometheus lookback) or extrapolate
(`rate` edges). Caveats are free-text strings with no location. Claude cannot see coverage at all.

Missingness is rarely random: the pod that stops reporting is often the sick one. A group band over
the survivors looks healthy *because* the sick members dropped out.

## 2. Goals and non-goals

Goals:
1. Every dataset carries, next to its values, typed **companion series** describing them; the first
   is `bucket_state` (coverage and trust per bucket).
2. Companions survive aggregation and op chaining by declared rules; never silently lost, never
   silently presumed valid.
3. Caveats are structured and **localized** (`where`): drawn on the graph only when present,
   always listed in the footer, visible to Claude and to the findings validator.
4. Missing/untrusted data is visible: stepped lines, coverage rug, heatmap column textures, group
   cloud thinning, window-view coverage badge, hover hints.
5. Each source adapter declares how its backend fabricates, drops or extrapolates data, verified by
   fixtures.

Non-goals (this spec): further companion kinds (`over_threshold`, `count` strip, estimator bounds)
— they get small add-on specs on this framework (beads `4ok.11/12/15/16/18`); tier-2 sandbox
propagation beyond the declaration contract; logs/non-Prometheus sources.

## 3. Series bundles

### 3.1 Shape

`FetchResult` becomes `Bundle`:

```
Bundle {
  primary:    pa.Table           # BUCKET_SCHEMA or distribution schema (unchanged)
  companions: {kind: pa.Table}   # keyed (ts_ms, series_id[, bucket]) on the primary's grid
  series:     pa.Table           # SERIES_SCHEMA + first_seen_ms, last_seen_ms, resolution_ms
  caveats:    list[Caveat]       # §4
}
```

`partial: int` on `FetchResult` is subsumed by `bucket_state` + `unknown` spans.

### 3.2 Companion registry

One entry per kind:

| member | contract |
|---|---|
| `schema` | Arrow schema of the companion table |
| `merge(members)` | combine across series for one bucket (group-by, `sum by`, group views) |
| `coarsen(buckets)` | combine across time (coarser step, LOD) |
| `caveats(table, series)` | produce localized caveats |
| `visuals` | default related marks (rug, strip, linked panel) |

### 3.3 Chaining through ops

Every op declares, per companion kind:

- `carry` — op doesn't change bucket meaning (filters, unit conversion)
- `recompute` — via `merge`/`coarsen` (group-by, resample, aggregation)
- `derive` — op computes it itself (e.g. `fraction_over` emits `over_threshold`)
- `drop` — removed, with caveat `companion_dropped:<kind>:<op>`

Undeclared ⇒ `drop` + caveat. A test enumerates registered ops × kinds and fails on undeclared
pairs, so new ops must decide. Lineage (MVP §3.1) means a derived point traces back to its inputs'
companion state.

An op may emit several kinds (latency `heatmap`: distribution + `bucket_state`; `fraction_over`:
`over_threshold` + `bucket_state`). The panel draws the primary mark plus each kind's related marks,
**only when the kind has something to say** (all-ok `bucket_state` draws nothing).

### 3.4 Claude

Per-kind compact summaries via the existing summary path (`core/summary.py`): for `bucket_state`
— coverage %, longest gap, silent members, resets, unknown spans with reasons. Bulk companion
tables never enter Claude's context.

## 4. Localized caveats

### 4.1 Model

```
Caveat {
  code:     str                       # registry key, e.g. missing_data, low_count, y_zoomed
  severity: info | warn | blocks_claim
  message:  str                       # human text, already scoped
  where:    { spans: [[t0, t1]]?, series: [id]?, values: [lo, hi]? } | null
  source:   bucket_state | validator | op:<name> | source_profile
}
```

`where = null` ⇒ whole panel (footer only).

### 4.2 Visual forms

| code | localizer |
|---|---|
| `missing_data`, `untrusted_data` (from `bucket_state` ≠ ok) | coverage rug, heatmap column textures, cloud thinning |
| `silent_members` | listed beside outliers + group rug row |
| `low_count` | faded segments/cells (exists) |
| `y_zoomed` | badge + context strip (exists) |
| `reset`, `interval_change` | ↺ marker on plot / tick on axis |
| `smoothed` | label on line + raw envelope beneath |
| `estimated_counts`, estimator bounds | interval bars / bound band |
| `companion_dropped` | footer only |

### 4.3 Footer and linking

Provenance footer lists every caveat. Hover footer entry ⇒ highlight its `where` on the graph; hover
a localized mark ⇒ show its caveat.

### 4.4 Findings

A finding whose claim window/series overlaps a `blocks_claim` caveat is rejected with a hint to
narrow scope. `warn` caveats in the window are copied into the finding's caveats. Thresholds for
promoting `missing_data` to `blocks_claim` (e.g. coverage < 50% in the claim window, or any
`unknown`) live with the validator rules.

### 4.5 Migration

Existing string caveats (`low_count`, `estimated_counts`, `metadata_coverage:…`, …) map to codes
with `where = null`. Producers add `where` as they are touched. API keeps a `caveats` list; UI
accepts both shapes during migration.

## 5. `bucket_state` (first companion)

### 5.1 Per series, per bucket

| field | meaning |
|---|---|
| `observed` | samples in the bucket (today's `count`) |
| `expected` | step / series resolution |
| `state` | `ok` · `partial` (observed/expected < 0.9) · `empty` (alive, 0 samples) · `absent` (outside first/last seen) · `unknown` (fetch failed, outside retention, source can't tell) |
| `flags` | bitmask: `reset`, `interval_change`, `stale_marker`, `source_filled` |
| `reason` | for `unknown`/`source_filled`: short code from the source profile or error |

Dataset level: failed spans with error text (never cached; retried on next read).

### 5.2 Merge across series (per bucket)

- `alive` = members not `absent`; `reporting` = members `ok` or `partial`
- `coverage` = Σobserved / Σexpected over alive
- `silent` = alive members that are `empty` → join the outlier set
- state: `unknown` if any member `unknown`; else `ok` if reporting = alive; else `partial`
  (`empty` if reporting = 0); flags OR-ed
- values computed over reporting < alive carry a `missing_data` caveat ("band over 41/44")

### 5.3 Coarsen across time

Sum `observed`, `expected`; OR flags. State is re-classified from the sub-bucket states, not from
the sums (re-applying jitter tolerance to sums would turn all-`ok` data `partial`): `unknown` if any
sub-bucket is `unknown`; `absent` only if every sub-bucket is absent (absent sub-buckets add nothing
to the sums); `empty` if nothing was observed; `partial` if any alive sub-bucket was `partial` or
`empty`; else `ok`. This keeps coarsening associative and consistent with §5.2.

### 5.4 Fetch failures

`SeriesCache.get` gathers chunks with `return_exceptions=True`; a failed chunk becomes an
`unknown` span (reason = error class/message) for every series in the query, is not stored, and is
retried on the next read. A query fails outright only if **every** chunk fails.

## 6. Source semantics

Each adapter declares a `MissingDataSemantics` profile; values come from `docs/data-source-quirks.md`
and are only trusted once verified there (✅ with fixture).

| property | used for |
|---|---|
| gap filling (lookback / fill policy) | choose queries that cannot fabricate; else `source_filled` |
| staleness markers | `stale_marker`; distinguish series-ended vs scrape-missed |
| rate edge handling (extrapolate / previous sample) | flag partial-window rates; post-gap spike risk (VQ2) |
| scrape-health signal (`up`) | refine `empty` reasons (target down) — later, optional |
| resolution tiers | `interval_change`; per-series `expected` |
| partial responses / warnings | `unknown` spans with source message |
| retention edge | `unknown` (not `empty`) before retention |

Adapter rules:
1. Values come from `*_over_time` / `rollup` over the step window, never instant evaluation;
   unavoidable filling ⇒ `source_filled`.
2. Can't tell ⇒ `unknown` with reason, never `ok`.
3. Profile summary shown on the source card and in the provenance footer.
4. Each profile property is pinned by recorded-fixture tests (`1h9.6`, `1h9.10`).

Known current gaps (evidence: `docs/data-source-quirks.md`, bead `1h9.10`): VictoriaMetrics
`increase`/`rate` carry a gap's whole increase into the first bucket after it (`1h9.13`); warnings /
`isPartial` are ignored (`1h9.12`). Profiles are in `sources/semantics.py`.

**Expression path observed counts (decision, `1h9.11`).** A non-selector expression's values come
from a subquery `(expr)[step:res]`; its `count_over_time` counts instant evaluations, which lookback
fills (`subquery_fills_gaps`, verified on Prometheus and VictoriaMetrics), so it is never used as
`observed`. Hybrid, in `sources/observed.py`:

- *Exact where derivable*: one vector selector, label-preserving functions (`rate`, `increase`,
  `*_over_time`, `abs`, `clamp`, `histogram_count`, ...) with literal other arguments, arithmetic
  with literals, aggregations sum/avg/min/max/count/group/stddev/stdvar (by/without), and
  `histogram_quantile(q, ...)` as an aggregation `without (le, vmrange)`. `count` comes from
  `count_over_time(sel[step])` lifted through the same aggregations with `sum`, so its series are
  the expression's own. For an aggregate, `observed` = samples of all members (member coverage
  stays unknown, as today).
  *Binary arithmetic* (`telemetry-nerd-mig`) between two or more derivable top-level vector
  operands (`a / b`, `rate(err[5m]) / rate(total[5m])`, `100 * (1 - a / b)`): `observed` per
  bucket is the smaller of the operands' observed sample counts (a ratio is only as observed as
  its sparser side); series present on only one side are not in the result, as they are not in
  the expression's. Operand count queries are deduplicated, then folded pairwise with
  `((x) <= (y)) or ((y) and (x))`; any underivable operand makes the whole expression cannot-tell.
  Because the min takes the smaller count, a `partial` bucket on the denser operand is hidden:
  only gaps on the sparser side (or full gaps) show.
  A value in a bucket with no observed sample is filled and dropped
  (bucket `empty`); samples without a value (e.g. `rate` with one sample) are dropped too, never
  counted `partial`. Cost: `fetch` runs the same number of queries, but the count query is no
  cheaper for a ratio: the fold repeats its operands (up to 2^(n-1) `count_over_time` calls for
  the first of n distinct operands), hence the cap below. `fetch_values` (quantile path) adds one
  count query and keeps only values of buckets that observed samples (masks lookback fill on
  summary/gauge series, VM previous-sample values, windows longer than the step).
- *Cannot tell otherwise* (filters/comparisons, `bool`, set operators, vector matching,
  `offset`/`@`, topk, label_replace, nested subqueries, binary ops wrapped in a function or
  aggregation such as `sum(a/b)`, `abs(a/b)`, `clamp_max(a/b, 1)`, `-(a/b)`, more than
  `MAX_FOLD_OPERANDS` (4) distinct operands or a count query over `MAX_COUNT_QUERY_LEN` (4096)
  characters, unknown functions): the adapter keeps the
  subquery count (it still weights the bucket mean), and `counts_are_observed(expr)` is False:
  consumers must mark those buckets `unknown` + `source_filled` (reason `subquery_fills_gaps`),
  never `ok`. Not derived from the profile: an evaluation count is never a sample count, even
  on a backend that did not fill.

## 7. Rendering

### 7.1 Time series and groups

- **Stepped paths by default** (each bucket = its interval; risers only between adjacent present
  buckets). Connected lines only on explicit request, with a caveat. Applies to Claude's `show()`.
- **Coverage rug** between the plot floor and the x-axis tick labels (small gap below the plot; tick labels stay last), drawn only when any plotted `bucket_state` ≠ ok:
  - rows: the plotted aggregate, plus one per drawn outlier or silent member
  - encoding: tint = ok · grey fill ∝ missing share = partial · solid grey = empty ·
    hatch = unknown · dotted = absent
  - ~8 px per row; the aggregate row always shows `unknown`
- **Discontinuities**: ↺ for `reset`, axis tick for `interval_change`.
- **Group views**: many series are never drawn as lines. Group = **density cloud** (canvas mark: x
  time, y value, intensity = members at that value, normalized by `alive`) + mean line + outliers.
  Silent members thin the cloud and are listed with the outliers ("3 silent"). Cloud design details
  (binning, colormap, outlier rule) belong to the group-view work; this spec fixes only the
  normalization-by-alive and silent-member rules.

### 7.2 Heatmaps

Rug as above, plus in-plot textures on missing columns: dots = `empty`, hatch = `unknown`. Blank
cell = measured, nothing happened. Partial columns faded like low-n cells.

### 7.3 Window views (histogram, ECDF, CCDF, threshold readout)

Coverage badge ("covers 87% of window · 5m missing · 2 resets"), with a tooltip listing missing steps; a mini rug only if real use shows the
tooltip is not enough.
Fractions/counts carry the caveat when coverage < 100%.

### 7.4 Hover hints

On rug cells and textured columns: span · state + reason (from source profile) · observed/expected
at resolution · last seen · for groups `reporting/alive` and silent ids.

### 7.5 Accessibility

Neutral greys/textures ≥ 3:1 against background in both themes; states distinguished by pattern
(solid / dots / hatch / dotted), never by colour alone.

## 8. Error handling

- Partial fetch failure → `unknown` spans, panel still renders, caveat `untrusted_data` with reason.
- All chunks fail → existing `SourceError` path.
- Source profile property unverified (❓/📖) → treated as "can't tell" for that property: affected
  buckets get `source_filled`/`unknown` conservatively, caveat `source_semantics_unverified`.
- Companion schema mismatch from a producer → op error (bug), not a silent drop.

## 9. Testing

- Property tests for `merge`/`coarsen`: associativity, commutativity, `unknown` absorbs, `absent`
  excluded from denominator, coverage bounds [0, 1].
- Op declaration completeness test (ops × kinds).
- Cache: partial chunk failure → `unknown` span, not cached, retried.
- Source fixtures per backend (from `1h9.10`): lookback fill, series end, reset, scrape failure,
  downsampled tier switch.
- UI unit: rug drawn iff any state ≠ ok; texture per state; isolated sample visible; stepped path
  geometry; hover content.
- Findings validator: claim over `blocks_claim` span rejected; `warn` caveats copied.
- E2E on local stack scenario (`1h9.5`): kill a target mid-window → rug shows `empty` for that
  series, group shows silent member, Claude summary reports the gap.

## 10. Phasing (for the plan)

1. Bundle + companion registry + `bucket_state` computation from existing `count` and resolution;
   op declarations; summaries. (No source-profile dependency.)
2. Structured caveats + footer index + findings rule; migration of string caveats.
3. Partial fetch failures → `unknown` spans.
4. Rendering: stepped paths, rug, heatmap textures, window badge, hover.
5. Source profiles once `1h9.10` answers land; `source_filled`, `stale_marker`, `interval_change`.
6. Group cloud normalization + silent members (with group-view work).

## 11. Open questions

- `partial` threshold 0.9: revisit after real data (scrape jitter, OTel push intervals; XQ1).
- Whether `up{}` is worth an extra query per panel to refine `empty` reasons.
- Group-cloud visual design (separate brainstorm).
