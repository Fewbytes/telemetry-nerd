# Bucket merging per expression kind — design

Bead `telemetry-nerd-7jme`. Status: design, not implemented. Direction agreed with the user
2026-10-04: **option 1 + option 2 for cheap cases** (bead text). Binding: `docs/principles.md`
(P3 never erode peaks, P6 bulk data stays out of context, P10 means and ratios merge only with
their weights, P11 missing is not zero, P16 results are model outputs, P17 a new model is fine
when implemented and used correctly and explained; labels must hold under the cautious model).
User decisions 2026-10-04 on §11: Q1, Q2, Q4, Q5, Q6 decided (folded in below); Q3 open.

**Terms** (`docs/glossary.md`): *series interval* (a series' natural sample spacing); *query
window* (the `[w]` of a range function); *query resolution* (evaluation spacing inside a query
bucket, the subquery `res`); *query step* (= query bucket width); *display bucket* (the width a
zoom/LOD merge produces); *time range* (start..end); *tile*: query window = query step **and** one
evaluation per query bucket (query resolution = query step, or values fetched at the step with
`fetch_values`), the only case where values sum; *effective time resolution* = max(query window,
query step, display bucket), floored by the series interval.

## 1. Problem

Every place that combines query buckets into display buckets (or across a time range) uses one rule: the **count-weighted mean** of bucket
`avg` (weight = sample count). That is the sample mean, right for a raw selector (a gauge read
as `rollup(x[query step])`). For anything else it is the wrong statistic.

Worked numbers (four 15 s query buckets merged into one 1 m display bucket; query resolution
15 s, so one evaluation per query bucket; a counter at 10/s that spikes to 30/s for 15 s; observed
sample counts 3, 3, 1, 1 because samples thinned while the target was busy):

| expression | bucket values | counts | count-weighted (today) | correct | correct rule |
|---|---|---|---|---|---|
| `rate(x[1m])` | 10, 10, 30, 10 /s | 3, 3, 1, 1 | (30+30+30+10)/8 = **12.5 /s** | **15 /s** | time mean |
| `increase(x[15s])` (tiles: query window = query step = query resolution) | 150, 150, 450, 150 | 3, 3, 1, 1 | 1500/8 = **187.5** | **900** in the minute (225 per 15 s) | sum |

The weights correlate with the value (fewer samples under load), so the error is a bias, not noise.

**Increase tiles at ~1 sample per query bucket: bias (1 + 2p).** Series interval = query step;
with probability `p` a sample lands in the neighbouring query bucket (the uup case): that bucket
holds two samples' worth, `(count 2, value 2I)`, and its neighbour `(count 0, value 0)` (kept by
`settle_unobserved`, `model/companions.py:321`). Per bucket, numerator
`(1-2p)·I + p·(2·2I) + p·0 = I(1+2p)`, denominator `(1-2p) + 2p + 0 = 1`: the count-weighted
mean reads `I(1+2p)`, the time mean reads `I`. Measured p ≈ 0.065 gives 1.13×; p → 0.5 gives
2×.

### Finding F1: a derived bucket is already a time mean

The adapter fetches a non-selector as `rollup((expr)[step:res])` (VictoriaMetrics) or
`*_over_time((expr)[step:res])` (Prometheus) (`sources/promql.py:226-249`; `res` is the query
resolution, the source's resolution): the query bucket `avg` is the mean of query step / query
resolution evaluations of the expression, and `count` is the *underlying selector's*
sample count (`observed_count_query`, `sources/observed.py:265`), not the number of evaluations.
Consequences:

1. For any derived expression whose text does not depend on the query step, the equal-weight
   (time) mean of the query buckets in a display bucket **is** the value the source returns with
   the display bucket as query step (exact when every query bucket was fully evaluated; otherwise off by at most the share of
   partially evaluated buckets, which `bucket_state` marks PARTIAL). Re-querying such an
   expression costs a source round trip to get a number we already have.
2. Re-querying changes the answer only when the expression changes with the query step: a
   `$__rate_interval` template, a tile, or a query window shorter than the new query step that
   should become a tile or widen. "Window matched to step" (bead) is that rewrite (§4).
4. A query window equal to the query step is **not** a tile when the query resolution is finer:
   `increase(x[1m])` at a 1 m query step and 15 s query resolution is the mean of 4 overlapping
   1 m windows, a sliding window; it merges by time mean, never by sum.
3. The count-weighted mean of a derived expression weights by samples of a different series
   than the one plotted. That is the root bug.

## 2. Merge kinds

One classifier, `merge_kind(expr, query_step_ms, query_resolution_ms, fetch, lookup)` →
`MergeKind(kind, query_window_ms, tile, requery, effective_ms)` (`fetch`: `rollup` or `values`), stored on the dataset at query time (§7). Rules are applied to the expression as
queried (after family and `$__rate_interval` expansion); the template is kept for re-query.

| kind | expressions | LOD line (one display bucket) | envelope | summary per series | re-query |
|---|---|---|---|---|---|
| `sample_mean` | a plain selector (`is_selector`) | Σ avg·count / Σ count (today) | min of min, max of max | `mean` (sample mean) | never |
| `counter_total` | a plain selector whose catalog type is `counter` | as `sample_mean` | same | `last`, `mean: null`, caveat `raw_counter` | never |
| `time_mean` | `rate`, `irate`, `deriv`; sliding query windows, i.e. every non-tile (§3); `avg_over_time`, `last_over_time`, `present_over_time`; instant arithmetic of those | equal-weight mean of the query buckets with a value | same | `mean` (time mean); `per_s` when it is a rate of a counter | never (F1.1) |
| `sum` | tiles only (query window = query step, one evaluation per query bucket) of `increase`, `increase_pure`, `delta`, `changes`, `resets`, `count_over_time`, `sum_over_time` | per-tile mean Σ avg / n (drawn, §6) and `total` Σ avg | min / max tile | `total`, `per_s`, `mean: null` | never |
| `max` / `min` | `max_over_time` / `min_over_time`, any query window | max of query-bucket `max` / min of query-bucket `min` (peak-preserving, P3) | same | `max` / `min`, `mean: null` | never |
| `ratio` | top-level `/` or `*` between two vector operands; `histogram_avg` | not merged locally | min / max | `mean: null`, `mean_unavailable: "ratio"` | when the rewrite changes the expression (§4); else `cannot_combine` |
| `quantile` | `exprkind.analyze(expr).quantile` | never merged (P10, today) | — | as today | when the rewrite changes the expression; else own step (today) |
| `unknown` | everything else (§3) | not merged locally | min / max | `mean: null`, `mean_unavailable: "kind_unknown"` | when rewritable; else `cannot_combine` |

The envelope (min of the query buckets' min, max of their max) is valid for every kind: it
describes the plotted query buckets, whatever the line means. That keeps the current min/max-preserving LOD (MVP spec §6.5)
untouched.

`sum` vs `time_mean` arithmetic is the same for the drawn line (mean per tile); they differ in what
the summary and the tooltip report (a total, §5) and in the label (§6).

## 3. Classification rules

Parsing reuses what exists; no second PromQL parser:

- `analysis/exprkind.py`: `_strip_comments`, `_mask_strings`, `_peel_parens`, `_close`,
  `_split_args`, `analyze` (quantiles, `exprkind.py:206`), `split_ratio` (`:357`),
  `counter_rate_source` (`:379`), `range_windows_ms` (`:67`), `expand`/`rate_interval_ms`
  (`:62-75`).
- `sources/observed.py`: the operand walker `_peel` / `_operands` / `_CALL` /
  `_TRAILING_CLAUSE` / `_LITERAL` / `_SELECTOR` and the function sets `_PRESERVING`,
  `_AGGREGATIONS` (`observed.py:35-160`). Move the walker primitives to `exprkind.py` (observed
  imports them) so both walkers share one tokenizer.
- `model/companions.py`: query-window detection (`_TILE_FUNCS`, `_top_level_bracket`,
  `_bracket_duration`, `companions.py:47-100, 212-280`). Lift `window_of_call(text, call)` out of
  `_unobserved_rule` and use it in both.
- Counter type from the catalog, as `signal_ops.events_scale` does (`core/signal_ops.py:170-183`).

**Leaves** (a function over a range of a plain selector, or a selector):

| leaf | kind |
|---|---|
| whole expression is a plain selector | `sample_mean` / `counter_total` |
| `rate`, `irate`, `deriv` (any query window) | `time_mean` |
| `increase`, `increase_pure`, `delta`, `changes`, `resets`, `count_over_time`, `sum_over_time` as a tile: query window = query step **and** (query resolution = query step, or `fetch == "values"`) | `sum` (tile) |
| the same otherwise: query window ≠ query step, or = query step with a finer query resolution (sliding) | `time_mean` (a level "per query window w"; summing overlapping windows counts each event w / query resolution times) |
| `max_over_time` / `min_over_time` | `max` / `min` |
| `avg_over_time`, `last_over_time`, `present_over_time` | `time_mean` |
| `stddev_over_time`, `stdvar_over_time`, `idelta`, `predict_linear`, `holt_winters`, `mad_over_time` | `unknown` |
| `quantile_over_time`, `histogram_quantile`, summary `{quantile=..}` | `quantile` |
| an instant selector inside a larger expression | `time_mean` (the rollup path evaluates it at the query resolution, F1) |

**Composition** (inner kind K):

| form | result |
|---|---|
| `sum [by\|without (..)] (X)`, trailing `by` too | K for `time_mean`, `sum`; `max`/`min` → `unknown` (sum of maxima) |
| `avg by (..) (X)` | K for `time_mean`, `sum` (model: membership constant within a display bucket; stated) |
| `max by (..) (X)` / `min by (..) (X)` | `max` for `max`, `min` for `min`, `time_mean` for `time_mean`, else `unknown` |
| `count`, `group` by | `time_mean` |
| `stddev`, `stdvar`, `topk`, `bottomk`, `quantile`, `count_values`, `limitk` | `unknown` |
| `X * c`, `c * X`, `X / c`, c a positive literal | K (so `rate(x[1m]) * 60` is `time_mean`, `increase(x[1m]) / 1e3` at a 1 m query step and 1 m query resolution is `sum`; at a 15 s query resolution it is `time_mean`) |
| negative literal factor | `max` ↔ `min`; others K |
| `X + c`, `X - c` | K for `time_mean`, `max`, `min`; `sum` → `unknown` |
| `X + Y`, `X - Y` (vectors) | K when both sides have the same K ∈ {`time_mean`, `sum`}; else `unknown` |
| `X / Y`, `X * Y` (vectors), `histogram_avg` | `ratio` |
| `%`, `^`, `atan2`, comparisons (with or without `bool`), `and`/`or`/`unless`, `on`/`ignoring`/`group_*` | `unknown` |
| label-preserving instant functions (`abs`, `clamp*`, `round`, `ceil`, `floor`, `sqrt`, `exp`, `ln`, `log*`, `sgn`, `label_replace`, `label_join`) | `time_mean` stays `time_mean` (the plotted level, F1.1); others `unknown` |
| `offset d` | transparent (a time shift) |
| `@ t` | inner K for `time_mean`/`max`/`min` (a constant over the range); `sum` → `unknown` (no tile) |
| subquery `(...)[r:s]` | `unknown` (bead: subqueries re-query) |
| anything the walker cannot read | `unknown` |

`requery` on the result: `rewrite` when the re-query rewrite (§4) changes the expression,
`none` when it does not, `impossible` for code outputs, filter outputs and binding outputs
(`meta.code_node`, `meta.derived`, `producer.kind == "binding"`).

Non-source datasets: a `filter()` output (`meta.derived`) inherits `time_mean` when its parent's
kind is `sample_mean`, `time_mean` or `sum` (linear filters over equal query steps), else `unknown`. A code
output is `unknown` unless its lineage declares a merge kind that is doable for its statistic
(decided, Q4; below). A binding `error_ratio`
(`core/binding_ops.py:379-401`) is `ratio`; it is served at its own query step today (it has an
interval, `core/service.py:2279-2283`) and stays so; a ratio of summed parents is a follow-up.

**Code output declarations (decided, Q4).** `Lineage` (and `tn` output declarations) gain an
optional `merge` per value column: `sample_mean` (needs per-bucket counts), `time_mean`, `sum`,
`max`, `min`, or for a distribution output `histogram` (bucket counts add). The declaration is
validated, never trusted: refused when the column's declared or catalog statistic is
non-mergeable (`catalog/mergeability.py`: percentile, median, MAD, IQR, truncated mean — e.g.
`time_mean` on a p99 column is rejected), `sample_mean` without counts is refused, `sum` on a
`unit="ratio"` column is refused. Undeclared or unsure → `cannot_combine` (P10, P17: a model is
used only within its assumptions). The declared kind appears in the code caveat text so the
merge is explained.

## 4. Re-query

**When (automatic).** Only for `ratio`, `quantile` and `unknown` datasets with
`requery == "rewrite"`, when the LOD factor is > 1 (more buckets than pixels). `sample_mean`,
`counter_total`, `time_mean`, `sum`, `max`, `min` are merged locally. `requery == "none"`
(query-step-invariant text): no round trip (decided, Q1); the line is the local time mean of the
fetched query buckets, which is what the source would return (F1.1), and the panel says so explicitly:
the merge `basis`/method text and a panel note "line: local time mean of the fetched {query step}
query buckets, not re-queried; [re-query at {S}]". `unknown` carries the caveat `merged_plotted_mean`;
`ratio` carries `cannot_combine` (a mean of ratios, P10) and first tries native resolution up to
`NATIVE_POINT_CAP` (4× width, at most 4000 points per series) before merging.

**Forced re-query (decided, Q1).** The user can always ask for the source's own value at the
display bucket width, whatever the kind (except code/filter/binding outputs, `requery ==
"impossible"`, where the control is disabled with the reason): a "re-query at {S}" action on
the panel note (UI) and `requery: bool` on `show`/panel data (MCP, `force_requery=true`). A forced
re-query of a query-step-invariant expression uses the rollup fetch with query step `S` (the expression as written);
of a rewritable one, `rewindow` + `fetch_values` as below. The result is a dataset (Q6).

**Choosing a query window (decided, Q2).** Whenever *we* choose the query window
(`$__rate_interval`, this rewrite), we prefer a tile: query window = query step, fetched with one
evaluation per query bucket, as long as each tile holds ≥ 2 samples (query step ≥ 2 × series
interval). Only when it would not do we widen, to `rate_interval_ms(step, series interval)`
(`exprkind.py:62`), and the panel names that query window. `choose_window(query_step,
series_interval) -> (query_window, tile: bool)` in `exprkind.py` is the one rule; `expand` and
`rewindow` use it. The series interval is the source's learned resolution
(`learn_resolution`, `sources/promql.py:797`) unless a per-series interval is known. A tile
chosen this way is fetched with one evaluation per query bucket: `rollup((expr)[step:step])`
(the adapter's `_window`, `promql.py:226-230`, takes the query resolution as a parameter
instead of always `self.resolution_ms`) or `fetch_values`. This changes what `$__rate_interval`
expands to for new queries (Grafana's `max(4 × res, step + res)` today, `exprkind.py:62-64`);
existing datasets keep their stored expression (§7).

**Rewrite** `rewindow(template, base_query_step, S, series_interval)` (new in `exprkind.py`,
generalising `core/profiles.py:79-135` `profile_target.widen`, which profiles then reuse), `S` the
re-query's query step (= the display bucket):

1. `$__rate_interval` → `choose_window(S, series_interval)`.
2. Query windows equal to the base query step (tiles as written) → `S` by the same rule.
3. Query windows of `rate`/`increase`/`delta`/`changes`/`*_over_time` shorter than `S` →
   `choose_window(S, ...)`.
4. Query windows ≥ `S`, `offset`, `@`, subquery ranges: unchanged.

Then fetch with `fetch_values` (`promql.py:429`): one evaluation per query bucket (query
resolution = query step), query window matched to it, so `increase(a[1m]) / increase(b[1m])` at 1 m becomes the ratio of the minute
sums at `S` — ratio of sums, P10. Quantiles use the existing quantile fetch (values + `count_expr`
for n, `core/service.py:520-536`). The line comes from the re-query; the envelope stays the local
min/max of the original query buckets. `S` is the smallest `_NICE_STEPS` value ≥ query step × LOD factor
that is a multiple of the dataset's query step (`core/service.py:174`), so resizes hit a few cached steps.

**Cached how.** Through `SeriesCache.get` with key `(source identity, expr_S, S)`
(`datasets/cache.py:47-55`; `values|` prefix as the quantile path uses). Separate chunks per `S`,
same settle/TTL rules; `peek` (`cache.py:133`) answers "already cached?" without a fetch. No
format change to what a chunk holds, so no `EXPR_FORMAT` (`\0v2`, `cache.py:19`) bump. The
fetch body is shared with `query()`: extract `_fetch(expr, rng, step)` from
`core/service.py:497-536` (cache + `settle_unobserved` or values + counts) so both paths stay
identical.

**Lineage (decided, Q6).** A re-query creates a **real dataset**, kept by default: citable,
`datasets.put` with `expr = expr_S`, `step_ms = S`, `expr_template` as the parent's, `parents =
[panel dataset]` and `producer = {"kind": "requery", "of": <dataset>, "step": S, "trigger":
"lod" | "user"}`; its `dataset.created` event is `internal` (as every fetch's), so Claude's feed
does not grow per zoom. The panel keeps its own dataset; it records the re-query datasets it has
drawn in `spec.views: {S: dataset_id}` (bounded; the newest few per panel), so a reload reuses
them. The payload stays small: `merge: {kind, how: "requery", dataset, expr, step_ms, status}`.
Cache hits still make a dataset (cheap: rows are already local) only on first use per (panel,
S); later draws reuse the recorded id.

**Dependency: fold / retire.** Nothing today folds or retires datasets or views: panels have only
`close` (`workspace/store.py:149`, `core/workspace_service.py:692`), datasets are immutable and
never hidden (P7). Re-query datasets would clutter `workspace_get`/the dataset list. Needed: a
soft `retired` flag on datasets (never deleted; evidence links keep working), set by "fold
re-query views" on a panel (UI) and a `retire` MCP op, and `workspace_get` listing re-query
datasets folded under their parent (`+3 re-query views`) unless cited. Plan Task 9 builds it; until
it lands, re-query datasets are listed folded by `producer.kind == "requery"`.

**Latency and UI.** `panel_data` stays synchronous for everything else. For a re-query kind:

- cached (`peek` covers the range, or `spec.views` has S): served in the same response,
  `status: "requeried"`;
- not cached: the response carries native resolution when within `NATIVE_POINT_CAP`, else the
  envelope only (no line), `status: "pending"`; one background re-query per (dataset, `S`),
  a newer `S` for the same panel supersedes it; on completion a UI-only socket frame
  `panel.view_ready {panel, step_ms}` (internal class, never in Claude's feed) makes the UI
  refetch. The badge reads "line: re-querying the source at 5 m".
- source unreachable / `SourceError` / `LimitExceeded`: `status: "failed"`, caveat
  `requery_failed` with the reason; envelope only (or native resolution within the cap). Never a
  count-weighted mean as a stand-in (P11: unknown is not a value).

The time-selector preview/rescope (`core/service.py:1044-1090`, spec
`2026-10-03-panel-time-selector-design.md` tier 2) already re-queries with query step `auto`, but from
`meta.expr`, which is stored *after* `$__rate_interval` expansion (`service.py:497` before
`datasets.put`). A `histogram_quantile(.., rate(x[$__rate_interval]))` panel previewed over 7 d
therefore keeps the 1 m query window at a 30 m query step. Fix: store `expr_template` on `DatasetMeta` and
re-expand it (preview, rescope, LOD re-query). Tier 1 (client-only viewport) does not re-render
yet (`ui/src/Panel.svelte:219-223`); when it does, it must ask the server for LOD over the viewport's
time range, never merge in the client.

## 5. Summary output per kind (MCP, `core/summary.py`)

Top level gains one small object; per series gains at most two numbers. With `top = 5` the
payload grows by under 200 bytes.

```json
"merge": {"kind": "sum", "tile": "15s", "basis": "sum of 15 s increase tiles; exact on VictoriaMetrics (prior-sample tiles partition the counter)"}
```

| kind | per series |
|---|---|
| `sample_mean` | `min`, `max`, `mean` (sample mean, today) |
| `counter_total` | `min`, `max`, `last`, `mean: null`; caveat `raw_counter` |
| `time_mean` | `min`, `max`, `mean` (time mean over buckets with a value); `per_s` for a rate of a counter |
| `sum` (tile) | `min`, `max` (per tile), `total` (Σ over observed tiles in the time range), `per_s` = total / observed seconds, `mean: null`; with gaps `total_lower_bound: true` for counters (a missing tile is unknown, never 0) |
| `time_mean` from a sliding count query window (`increase(x[5m])` at a 15 s query step, or `increase(x[1m])` at 1 m with 15 s query resolution) | `mean` (per query window w), `per_s` = mean / w; `total` ≈ per_s × observed seconds with basis "estimated from overlapping query windows" |
| `max` / `min` | `max` / `min`, `mean: null` |
| `ratio`, `unknown` | `min`, `max`, `mean: null`, `mean_unavailable` (`ratio` / `kind_unknown` / `code_output`) |
| `quantile` | today |

Caveats added: `tiles_extrapolated` (sum of tiles on Prometheus, which extrapolates within each
query window: approximate, `docs/data-source-quirks.md:121-127`), `time_mean_partial` (some query buckets
partially evaluated: the time weights are approximate, bounded by their share). `counts_unknown`
stays for code outputs. The `mean` key keeps its name for `sample_mean` and `time_mean` so
existing consumers (finding statistics, `tn` library) still read a mean where one exists, and get
`null` where today they get a wrong number.

The `basis` text always says what was done (P17): "time mean of the fetched 15 s query buckets,
computed locally, not re-queried" / "re-queried at 5 m (dataset d42)" / "declared by code node
c3: sum". `query` and `show` accept `force_requery` (Q1); the summary of a re-query dataset is an
ordinary summary of that dataset (its `producer` names the parent), so no payload grows. The
`merge` object carries `effective_time_resolution` (e.g. `"5m"`) and, when it exceeds the query
step, `query_window`.

## 6. UI labelling, increase panels on zoom

**Effective time resolution (decided, Q2).** Every panel states it: max(query window, query step,
display bucket), floored by the series interval, as "detail shorter than {X} is smoothed", naming
the query window when it exceeds the query step ("rate over 5 m windows"). It changes with zoom
(the display bucket) and is part of the `describeShown` sentence.

The display bucket changes with zoom and with panel width (`lod` factor = buckets / width,
`analysis/resample.py:62-69`), so whatever an increase panel draws must not change meaning when
the user resizes the browser.

Options: (a) sum per displayed bucket; (b) per-second rate; (c) **per original tile**: the mean
of the tiles in each displayed bucket, in the tile's unit.

**Recommendation: (c).** At native zoom it is exactly the value the user queried (150 per 15 s);
zoomed out it reads 225 per 15 s for the spiky minute, the same unit, so the line does not jump
by ×k on a resize; the envelope (smallest/largest tile) is in the same unit as the line (with (a)
it would not be); the normal band (the operating profile's hourly value is also a per-tile mean,
`core/profiles.py:79-135`), the last-week ghost, indexed baselines and limit lines stay
comparable; `events_per_step` (`exprkind.py:393`) stays valid for the series merged into display buckets, so the
departure-from-zero and verdict counts downstream need no special case. (a) changes every
y-value ×k per zoom and breaks those comparisons; (b) changes the number the user wrote and
makes native zoom (per tile) disagree with zoomed out (per second) unless the panel always shows
per second, which is a different chart from the one asked for.

The total is not lost: each displayed point carries `total` and `tiles` (query buckets merged),
and the tooltip reads "900 in this 1 m bucket (4 × 15 s tiles); 225 per 15 s, 15 /s".

`describeShown` (`ui/src/lib/panelNotes.ts:242-299`), the panel's one-line "what is drawn":

| kind | text (step = display bucket, tile = the dataset's query step) |
|---|---|
| `sample_mean` | "Average of the samples per {step} bucket (line), min–max (band)." (today's text, made explicit) |
| `time_mean` | "Time average per {step} bucket of the value (line), min–max (band)." |
| `sum` | "Increase per {tile} (mean of the {tile} tiles in each {step} bucket; hover for the bucket total), smallest–largest tile (band)." At native zoom: "Increase per {tile} tile." |
| `max` / `min` | "Largest / smallest value in each {step} bucket (line = band edge)." |
| local time mean (`requery == "none"`) | "… local time mean of the fetched {query step} query buckets (not re-queried)" with a "re-query at {S}" action (Q1) |
| re-queried | "… re-queried from the source at {S} (dataset {id}): {expr_S}" plus the pending/failed badge |
| `cannot_combine` / envelope only | "Range of the values in each {step} bucket (band); no line: {why}. Zoom in for the source's own values." |

## 7. Migration and compatibility

- `DatasetMeta` (JSON, `datasets/store.py:21-61`) gains optional `expr_template: str | None` and
  `merge: dict | None` (the `MergeKind`, classified at query time with the catalog as of then, as
  `semantics_flags` is). Old datasets: `merge` is computed lazily from `meta.expr`; with no
  template, query windows are read literally (tiles still detected: query window = query step and
  query resolution = query step, from `meta.resolution_ms`). No schema change,
  no cache change, no salt bump.
- `DatasetMeta.producer` gains the `requery` kind (parents = the panel dataset) and an optional
  `retired: bool` (default false; §4 fold/retire). `Lineage` gains optional per-column `merge`
  (Q4). All optional JSON fields: old metas load unchanged.
- New queries with `$__rate_interval` may get a tile instead of Grafana's window (§4): a
  different cache key (the expanded expression differs), so no stale chunk is reused; panels
  re-queried after the change show the effective time resolution they now have.
- `rebucket(buckets, new_step, kind)` gains `kind`; the default stays `sample_mean` for one
  release so unconverted callers keep today's behaviour, then the default is removed (the plan's
  last task) so no caller is left on the implicit count-weighted mean.
- Existing findings cite statistics computed before the change; they are not recomputed. Their
  `mean` stays as recorded. Re-running `analyze`/`summary` gives the corrected numbers, a new look
  (P14).
- `catalog/mergeability.py` keeps the statistic-level table; this design is the expression-level
  counterpart. `docs/telemetry-graphing-guide.md` §5 ("mean | only with count | count-weighted")
  gains the time-mean row; MVP spec §6.5 and `skills/charting` mention the kinds.

## 8. Every place buckets are combined across time

Affected (merges across time with the count-weighted mean today):

| site | what | change |
|---|---|---|
| `core/summary.py:213-262` | per-series `mean` | per kind (§5) |
| `analysis/resample.py:15-69` (`rebucket`, `lod`) | the LOD merge | takes `kind` |
| `core/service.py:2278-2287, 2311` | time panel LOD (quantile, interval exempt) | kind from meta; re-query path (§4) |
| `core/panel_payloads.py:331-345` (`reference_series`) | previous/week references, ghost | the panel's kind (pointwise index needs like with like) |
| `core/panel_payloads.py:376-395` (`signal_payload`) | raw / removed of a filter panel | raw: dataset kind; removed: `time_mean` |
| `core/panel_payloads.py:480-486` (`limit_payload`) | limit / context lines | the line dataset's own kind |
| `core/signal_ops.py:207-210, 233-234` (`_prepare`) | coarsening for `analyze`, `spectrum`, spectrogram, `filter` (caveat `coarsened`) | unit-preserving: `time_mean` arithmetic for `time_mean`/`sum` (keeps `events_scale` valid), sample mean for selectors; `ratio`/`unknown`/`quantile` refuse to coarsen (hint: query at a coarser step) |
| `core/fleet_ops.py:153-156` | fleet coarsening to 1440 steps | as `_prepare` |
| `charts/indexed.py:21-28` (`window_baselines`), used `:54` and `panel_payloads.py:357` | window baseline for indexed view | sample mean / time mean (per tile for `sum`: indexing is a ratio of levels in one unit); refuse for `ratio`/`unknown` like quantiles |
| `analysis/profile_reference.py:21-55` (`hourly_means`; callers `panel_payloads.py:304`, `service.py:1522`) | panel rolled to hours to compare with the profile | time mean for derived kinds (the profile's hourly value is a rollup mean, F1), sample mean for selectors; `null` counts no longer default to weight 1 |

Fine (uses per-bucket values, or merges something additive):

| site | why |
|---|---|
| `core/summary.py:269-327` `_summarize_quantile` | no mean; min/max over meaningful buckets; n summed |
| `core/summary.py:343-427` `summarize_distribution`, `analysis/distlod.py`, `panel_payloads.py:189-201` heatmap | histogram counts summed (additive) |
| `model/bucket_state.py:719-747` `coarsen` | coverage (observed/expected sums), not values |
| `analysis/seasonal.py`, `core/seasonal_ops.py:140` | per-step values with equal weights (a time mean already); refuses rather than coarsens |
| `analysis/verdicts.py:462, 496-512` | sums of events per step (`events_per_step`): additive |
| `analysis/littles.py`, `core/littles_ops.py` | own tile fetches; windows averaged with equal weights by design |
| `analysis/profile.py`, `core/profiles.py:418-434` | the source rolls up to hours (a re-query, windows widened by `profile_target`) |
| `analysis/marginal.py`, `spc.py`, `excursion.py`, `spectrum.py`, `diagnostics.py`, `fleet.py` (analysis) | per-bucket values; affected only through `_prepare` / fleet coarsening above |
| UI (`ui/src/chart/*`) | no client-side merging today |

## 9. Test plan

Unit and e2e use fixtures only (no live VictoriaMetrics); integration tests may start the VM
container (`tests/integration/conftest.py`).

- **Classifier** (`tests/unit/test_merge_kind.py`): a table of ~60 expressions → kind, tile,
  requery, including every row of §3; `sum by (job) (rate(x[1m]))` → `time_mean`;
  `rate(x[1m]) * 60` → `time_mean`; `increase(x[1m])` at a 1 m query step with 1 m query resolution → `sum`, with 15 s query
  resolution → `time_mean` (not a tile), at a 15 s query step → `time_mean`;
  `a / b` → `ratio`; `x offset 1h` → `time_mean` (not a plain selector for `is_selector`,
  `promql.py:46`, so it is fetched through the rollup path); `@`; subqueries; quoted `/` and `#`
  inside label values never change the kind.
- **Merge arithmetic** (`test_resample.py`): the §1 worked numbers (12.5 → 15; 187.5 → 225 per
  tile, total 900); the (1 + 2p) fixture (spill/neighbour pairs) reads `I` under `sum`; gaps are
  excluded, never zero; NaN handling unchanged; envelope unchanged for every kind.
- **Summary** (`test_summary.py`): output shape per kind; payload size with 5 series ≤ today + 200
  bytes; `total_lower_bound` with a gap; `mean: null` + `mean_unavailable` for ratio/unknown.
- **Rewrite** (`test_rewindow.py`): golden strings for template expansion, tile → S when
  S ≥ 2 × series interval, widen otherwise, untouched query windows ≥ S, `offset`, `@`; `profile_target` unchanged by the move.
- **Re-query path** (`test_lod_requery.py`): a fake source (`tests/unit/fakes.py`) counting
  calls: first `panel_data` → `pending` + envelope; background completes → socket frame →
  second call `requeried` with zero source calls (cache); a different width mapping to the same
  `S` hits the cache; a failing source → `failed` + `requery_failed`, no line; code output →
  `cannot_combine`, no fetch; a completed re-query is a dataset with `producer.kind == "requery"`,
  parent = the panel dataset, an `internal` event, recorded in `spec.views` and reused on reload;
  `force_requery` on a `time_mean` panel fetches once and labels the line re-queried.
- **Code declarations** (`test_code_outputs.py`): `time_mean` on a percentile column refused;
  `sample_mean` without counts refused; `histogram` on a distribution accepted; undeclared →
  `cannot_combine`.
- **Fold/retire** (`test_dataset_retire.py`): retiring hides from `workspace_get`'s list but keeps
  `datasets.get` and evidence links working; folded count per parent; cited datasets never fold.
- **Callers**: indexed baselines, `hourly_means`, `_prepare` coarsening (events scale × kind),
  fleet coarsening, references and limit lines each get one test pinning the kind they use.
- **UI** (vitest): `describeShown` per kind, with the effective time resolution sentence and the
  query window named when it exceeds the query step; tooltip total for `sum`; pending/failed badges.
  Playwright e2e on the fixture source: an increase panel keeps its y-values when the panel is
  resized; a ratio panel shows the pending badge, then the line.
- **Integration** (VM container): for a jittered counter, local `sum` of 15 s tiles (15 s query
  resolution) equals `increase(x[1m])` fetched as values at a 1 m query step (VM, exact); the
  rollup fetch of `increase(x[1m])` at 1 m with 15 s query resolution does not (sliding); local `time_mean` of `rate(x[1m])` equals the VM rollup with
  the display bucket as query step within 1e-9 relative when every bucket is fully evaluated; a ratio re-query
  equals the ratio of summed tiles.

## 10. Risks

- **Classifier errors** move a panel between kinds. Mitigation: unknown is the default; every
  kind but `unknown` must be proven by the walker; the table test is the contract.
- **Source load** from LOD re-queries on resize. Mitigation: only `ratio`/`quantile`/`unknown`
  with `rewrite`; quantised `S`; one in-flight per panel; the cache; the existing politeness gate.
- **Time-mean model** (F1) assumes full evaluation per bucket; partially evaluated buckets bias
  the weights slightly. Stated in the merge basis; `time_mean_partial` when bucket_state shows
  PARTIAL buckets in a merge.
- **Prometheus tiles** extrapolate: `sum` is approximate there (`tiles_extrapolated`).
- **Behaviour change**: panels and summaries that showed a (wrong) mean now show a different number
  or `null`. Findings keep their recorded numbers; the change log/skill says why.
- **Dataset growth** from kept re-queries (Q6): one dataset per (panel, S) on first use, a bounded
  `spec.views`, internal events only, folded listing; retire is soft.
- **Envelope-only views** are new in the chart; they must not read as "no data" (hatching is
  reserved for gaps).

## 11. Decisions and open questions

Decided by the user 2026-10-04:

1. **F1 shortcut: yes.** Step-invariant derived expressions are merged locally as a time mean,
   stated explicitly (basis text, panel note), with a user control to force a re-query at the
   display bucket width (UI action, MCP `force_requery`) (§4, §5, §6).
2. **Sliding windows: resolved.** Values sum only for tiles under the exact definition (query
   window = query step and one evaluation per query bucket); every sliding query window merges by
   time mean. When we choose the query window we prefer tiles with ≥ 2 samples of the series
   interval each, widen only otherwise, and every panel states its effective time resolution
   (§3, §4, §6).
4. **Code outputs** may declare a merge kind only when it is doable for that statistic
   (histograms merge buckets; counts, sums, min, max; means with their weights); declarations are
   validated; unsure → `cannot_combine` (§3).
5. **Principle 10 amended** (means and ratios merge only with their weights: sample counts for raw
   samples, covered time for derived values; ratio of sums) and **principle 17 added** (all models
   are wrong, some are useful: new models are allowed when implemented and used correctly within
   their assumptions, and explained) in `docs/principles.md`.
6. **Re-query results are kept** as real, citable datasets with lineage to the panel dataset;
   the user can fold or retire them. Fold/retire does not exist yet (only panel close): built in
   the plan (§4 dependency).

Still open:

3. **Increase on zoom**: per original tile (recommended, §6) vs per second vs sum per displayed
   bucket.
