# Bucket merging per expression kind — design

Bead `telemetry-nerd-7jme`. Status: design, not implemented. Direction agreed with the user
2026-10-04: **option 1 + option 2 for cheap cases** (bead text). Binding: `docs/principles.md`
(P3 never erode peaks, P6 bulk data stays out of context, P10 only mergeable statistics merge,
P11 missing is not zero, P16 results are model outputs; labels must hold under the cautious
model).

## 1. Problem

Every place that combines buckets across time uses one rule: the **count-weighted mean** of bucket
`avg` (weight = sample count). That is the sample mean, right for a raw selector (a gauge read
as `rollup(x[step])`). For anything else it is the wrong statistic.

Worked numbers (four 15 s buckets merged into one 1 m bucket; a counter at 10/s that spikes to
30/s for 15 s; observed sample counts 3, 3, 1, 1 because scrapes thinned while the target was busy):

| expression | bucket values | counts | count-weighted (today) | correct | correct rule |
|---|---|---|---|---|---|
| `rate(x[1m])` | 10, 10, 30, 10 /s | 3, 3, 1, 1 | (30+30+30+10)/8 = **12.5 /s** | **15 /s** | time mean |
| `increase(x[15s])` (tiles) | 150, 150, 450, 150 | 3, 3, 1, 1 | 1500/8 = **187.5** | **900** in the minute (225 per 15 s) | sum |

The weights correlate with the value (fewer scrapes under load), so the error is a bias, not noise.

**Increase tiles at ~1 sample per bucket: bias (1 + 2p).** Scrape interval = step; with
probability `p` a scrape lands in the neighbouring bucket (the uup case): that bucket holds two
scrapes' worth, `(count 2, value 2I)`, and its neighbour `(count 0, value 0)` (kept by
`settle_unobserved`, `model/companions.py:321`). Per bucket, numerator
`(1-2p)·I + p·(2·2I) + p·0 = I(1+2p)`, denominator `(1-2p) + 2p + 0 = 1`: the count-weighted
mean reads `I(1+2p)`, the time mean reads `I`. Measured p ≈ 0.065 gives 1.13×; p → 0.5 gives
2×.

### Finding F1: a derived bucket is already a time mean

The adapter fetches a non-selector as `rollup((expr)[step:res])` (VictoriaMetrics) or
`*_over_time((expr)[step:res])` (Prometheus) (`sources/promql.py:226-249`): the bucket `avg` is
the mean of `step/res` evaluations of the expression, and `count` is the *underlying selector's*
sample count (`observed_count_query`, `sources/observed.py:265`), not the number of evaluations.
Consequences:

1. For any derived expression whose text does not depend on the step, the equal-weight (time)
   mean of the fine buckets **is** the value the source returns at the coarser step (exact when
   every fine bucket was evaluated `step/res` times; otherwise off by at most the share of
   partially evaluated buckets, which `bucket_state` marks PARTIAL). Re-querying such an
   expression costs a source round trip to get a number we already have.
2. Re-querying changes the answer only when the expression changes with the step: a
   `$__rate_interval` template, a tile (window = step) or a window shorter than the new step that
   should widen. "Window matched to step" (bead) is that rewrite.
3. The count-weighted mean of a derived expression weights by samples of a different series
   than the one plotted. That is the root bug.

## 2. Merge kinds

One classifier, `merge_kind(expr, step_ms, lookup)` → `MergeKind(kind, window_ms, tile,
requery)`, stored on the dataset at query time (§7). Rules are applied to the expression as
queried (after family and `$__rate_interval` expansion); the template is kept for re-query.

| kind | expressions | LOD line (one coarse bucket) | envelope | summary per series | re-query |
|---|---|---|---|---|---|
| `sample_mean` | a plain selector (`is_selector`) | Σ avg·count / Σ count (today) | min of min, max of max | `mean` (sample mean) | never |
| `counter_total` | a plain selector whose catalog type is `counter` | as `sample_mean` | same | `last`, `mean: null`, caveat `raw_counter` | never |
| `time_mean` | `rate`, `irate`, `deriv`; sliding windows (§3); `avg_over_time`, `last_over_time`, `present_over_time`; instant arithmetic of those | equal-weight mean of fine buckets with a value | same | `mean` (time mean); `per_s` when it is a rate of a counter | never (F1.1) |
| `sum` | tiles (window = step) of `increase`, `increase_pure`, `delta`, `changes`, `resets`, `count_over_time`, `sum_over_time` | per-tile mean Σ avg / n (drawn, §6) and `total` Σ avg | min / max tile | `total`, `per_s`, `mean: null` | never |
| `max` / `min` | `max_over_time` / `min_over_time`, any window | max of fine `max` / min of fine `min` (peak-preserving, P3) | same | `max` / `min`, `mean: null` | never |
| `ratio` | top-level `/` or `*` between two vector operands; `histogram_avg` | not merged locally | min / max | `mean: null`, `mean_unavailable: "ratio"` | when the rewrite changes the expression (§4); else `cannot_combine` |
| `quantile` | `exprkind.analyze(expr).quantile` | never merged (P10, today) | — | as today | when the rewrite changes the expression; else own step (today) |
| `unknown` | everything else (§3) | not merged locally | min / max | `mean: null`, `mean_unavailable: "kind_unknown"` | when rewritable; else `cannot_combine` |

The envelope (min of fine min, max of fine max) is valid for every kind: it describes the plotted
fine series, whatever the line means. That keeps the current min/max-preserving LOD (MVP spec §6.5)
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
- `model/companions.py`: tile detection (`_TILE_FUNCS`, `_top_level_bracket`,
  `_bracket_duration`, `companions.py:47-100, 212-280`). Lift `window_of_call(text, call)` out of
  `_unobserved_rule` and use it in both.
- Counter type from the catalog, as `signal_ops.events_scale` does (`core/signal_ops.py:170-183`).

**Leaves** (a function over a range of a plain selector, or a selector):

| leaf | kind |
|---|---|
| whole expression is a plain selector | `sample_mean` / `counter_total` |
| `rate`, `irate`, `deriv` (any window) | `time_mean` |
| `increase`, `increase_pure`, `delta`, `changes`, `resets`, `count_over_time`, `sum_over_time` with window = dataset step | `sum` (tile) |
| the same with window ≠ step (sliding) | `time_mean` (a level "per window w"; summing overlapping windows double-counts w/step times) |
| `max_over_time` / `min_over_time` | `max` / `min` |
| `avg_over_time`, `last_over_time`, `present_over_time` | `time_mean` |
| `stddev_over_time`, `stdvar_over_time`, `idelta`, `predict_linear`, `holt_winters`, `mad_over_time` | `unknown` |
| `quantile_over_time`, `histogram_quantile`, summary `{quantile=..}` | `quantile` |
| an instant selector inside a larger expression | `time_mean` (the rollup path evaluates it every `res`, F1) |

**Composition** (inner kind K):

| form | result |
|---|---|
| `sum [by\|without (..)] (X)`, trailing `by` too | K for `time_mean`, `sum`; `max`/`min` → `unknown` (sum of maxima) |
| `avg by (..) (X)` | K for `time_mean`, `sum` (model: membership constant within a coarse bucket; stated) |
| `max by (..) (X)` / `min by (..) (X)` | `max` for `max`, `min` for `min`, `time_mean` for `time_mean`, else `unknown` |
| `count`, `group` by | `time_mean` |
| `stddev`, `stdvar`, `topk`, `bottomk`, `quantile`, `count_values`, `limitk` | `unknown` |
| `X * c`, `c * X`, `X / c`, c a positive literal | K (so `rate(x[1m]) * 60` is `time_mean`, `increase(x[1m]) / 1e3` at 1 m is `sum`) |
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
kind is `sample_mean`, `time_mean` or `sum` (linear filters over equal steps), else `unknown`. A code
output is `unknown` unless its lineage declares `merge` (Q4). A binding `error_ratio`
(`core/binding_ops.py:379-401`) is `ratio`; it is served at its own step today (it has an
interval, `core/service.py:2279-2283`) and stays so; a ratio of summed parents is a follow-up.

## 4. Re-query

**When.** Only for `ratio`, `quantile` and `unknown` datasets with `requery == "rewrite"`, when
the LOD factor is > 1 (more buckets than pixels). `sample_mean`, `counter_total`, `time_mean`,
`sum`, `max`, `min` are always merged locally. `requery == "none"` (step-invariant text): no
round trip; `time_mean` arithmetic of the plotted value is what the source would return
(F1.1), drawn with the caveat `merged_plotted_mean` for `unknown`, and with `cannot_combine` for
`ratio` (a mean of ratios, P10) — for those two the panel first tries native resolution up to
`NATIVE_POINT_CAP` (4× width, at most 4000 points per series) before merging.

**Rewrite** `rewindow(template, S, res)` (new in `exprkind.py`, generalising
`core/profiles.py:79-135` `profile_target.widen`, which profiles then reuse):

1. Expand `$__rate_interval` at `S` (`expand`, `exprkind.py:72`).
2. Tile windows (= the dataset step) → `S`.
3. Windows of `rate`/`increase`/`delta`/`changes`/`*_over_time` shorter than `S` → `S`
   (`rate`/`irate`/`deriv`: `rate_interval_ms(S, res)`, as profiles widen).
4. Windows ≥ `S`, `offset`, `@`, subquery ranges: unchanged.

Then fetch with `fetch_values` (`promql.py:429`): one evaluation per coarse step, window
matched to it, so `increase(a[1m]) / increase(b[1m])` at 1 m becomes the ratio of the minute
sums at `S` — ratio of sums, P10. Quantiles use the existing quantile fetch (values + `count_expr`
for n, `core/service.py:520-536`). The line comes from the re-query; the envelope stays the local
min/max of the fine buckets. `S` is the smallest `_NICE_STEPS` value ≥ step × factor that is a
multiple of the dataset step (`core/service.py:174`), so resizes hit a few cached steps.

**Cached how.** Through `SeriesCache.get` with key `(source identity, expr_S, S)`
(`datasets/cache.py:47-55`; `values|` prefix as the quantile path uses). Separate chunks per `S`,
same settle/TTL rules; `peek` (`cache.py:133`) answers "already cached?" without a fetch. No
format change to what a chunk holds, so no `EXPR_FORMAT` (`\0v2`, `cache.py:19`) bump. The
fetch body is shared with `query()`: extract `_fetch(expr, rng, step)` from
`core/service.py:497-536` (cache + `settle_unobserved` or values + counts) so both paths stay
identical.

**Lineage.** A LOD re-query is a **view**, not a dataset: no `datasets.put`, no
`dataset.created` event, no new id (P7: the panel's evidence is still its dataset at its step).
The payload names it: `merge: {kind, how: "requery", expr: expr_S, step_ms: S, status}`. A number
read off it is not citable; to cite, Claude runs `query(expr_S, step=S)` (a real dataset), which
the MCP hint says.

**Latency and UI.** `panel_data` stays synchronous for everything else. For a re-query kind:

- cached (`peek` covers the range): served in the same response, `status: "requeried"`;
- not cached: the response carries native resolution when within `NATIVE_POINT_CAP`, else the
  envelope only (no line), `status: "pending"`; one background re-query per (dataset, `S`),
  a newer `S` for the same panel supersedes it; on completion a UI-only socket frame
  `panel.view_ready {panel, step_ms}` (internal class, never in Claude's feed) makes the UI
  refetch. The badge reads "line: re-querying the source at 5 m".
- source unreachable / `SourceError` / `LimitExceeded`: `status: "failed"`, caveat
  `requery_failed` with the reason; envelope only (or native resolution within the cap). Never a
  count-weighted mean as a stand-in (P11: unknown is not a value).

The time-selector preview/rescope (`core/service.py:1044-1090`, spec
`2026-10-03-panel-time-selector-design.md` tier 2) already re-queries at `step=auto`, but from
`meta.expr`, which is stored *after* `$__rate_interval` expansion (`service.py:497` before
`datasets.put`). A `histogram_quantile(.., rate(x[$__rate_interval]))` panel previewed over 7 d
therefore keeps the 1 m window at a 30 m step. Fix: store `expr_template` on `DatasetMeta` and
re-expand it (preview, rescope, LOD re-query). Tier 1 (client-only viewport) does not re-render
yet (`ui/src/Panel.svelte:219-223`); when it does, it must ask the server for LOD at the viewport,
never merge in the client.

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
| `sum` (tile) | `min`, `max` (per tile), `total` (Σ over observed tiles), `per_s` = total / observed seconds, `mean: null`; with gaps `total_lower_bound: true` for counters (a missing tile is unknown, never 0) |
| `time_mean` from a sliding count window (`increase(x[5m])` at 15 s) | `mean` (per window w), `per_s` = mean / w; `total` ≈ per_s × observed seconds with basis "estimated from overlapping windows" |
| `max` / `min` | `max` / `min`, `mean: null` |
| `ratio`, `unknown` | `min`, `max`, `mean: null`, `mean_unavailable` (`ratio` / `kind_unknown` / `code_output`) |
| `quantile` | today |

Caveats added: `tiles_extrapolated` (sum of tiles on Prometheus, which extrapolates within each
window: approximate, `docs/data-source-quirks.md:121-127`), `time_mean_partial` (some buckets
partially evaluated: the time weights are approximate, bounded by their share). `counts_unknown`
stays for code outputs. The `mean` key keeps its name for `sample_mean` and `time_mean` so
existing consumers (finding statistics, `tn` library) still read a mean where one exists, and get
`null` where today they get a wrong number.

## 6. UI labelling, increase panels on zoom

The displayed bucket changes with zoom and with panel width (`lod` factor = buckets / width,
`analysis/resample.py:62-69`), so whatever an increase panel draws must not change meaning when
the user resizes the browser.

Options: (a) sum per displayed bucket; (b) per-second rate; (c) **per original tile**: the mean
of the tiles in each displayed bucket, in the tile's unit.

**Recommendation: (c).** At native zoom it is exactly the value the user queried (150 per 15 s);
zoomed out it reads 225 per 15 s for the spiky minute, the same unit, so the line does not jump
by ×k on a resize; the envelope (smallest/largest tile) is in the same unit as the line (with (a)
it would not be); the normal band (the operating profile's hourly value is also a per-tile mean,
`core/profiles.py:79-135`), the last-week ghost, indexed baselines and limit lines stay
comparable; `events_per_step` (`exprkind.py:393`) stays valid for the coarsened series, so the
departure-from-zero and verdict counts downstream need no special case. (a) changes every
y-value ×k per zoom and breaks those comparisons; (b) changes the number the user wrote and
makes native zoom (per tile) disagree with zoomed out (per second) unless the panel always shows
per second, which is a different chart from the one asked for.

The total is not lost: each displayed point carries `total` and `tiles` (fine buckets merged),
and the tooltip reads "900 in this 1 m bucket (4 × 15 s tiles); 225 per 15 s, 15 /s".

`describeShown` (`ui/src/lib/panelNotes.ts:242-299`), the panel's one-line "what is drawn":

| kind | text (step = displayed, tile = dataset step) |
|---|---|
| `sample_mean` | "Average of the samples per {step} bucket (line), min–max (band)." (today's text, made explicit) |
| `time_mean` | "Time average per {step} bucket of the value (line), min–max (band)." |
| `sum` | "Increase per {tile} (mean of the {tile} tiles in each {step} bucket; hover for the bucket total), smallest–largest tile (band)." At native zoom: "Increase per {tile} tile." |
| `max` / `min` | "Largest / smallest value in each {step} bucket (line = band edge)." |
| re-queried | "… re-queried from the source at {S}: {expr_S}" plus the pending/failed badge |
| `cannot_combine` / envelope only | "Range of the values in each {step} bucket (band); no line: {why}. Zoom in for the source's own values." |

## 7. Migration and compatibility

- `DatasetMeta` (JSON, `datasets/store.py:21-61`) gains optional `expr_template: str | None` and
  `merge: dict | None` (the `MergeKind`, classified at query time with the catalog as of then, as
  `semantics_flags` is). Old datasets: `merge` is computed lazily from `meta.expr`; with no
  template, windows are read literally (tiles still detected by window = step). No schema change,
  no cache change, no salt bump.
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
  `rate(x[1m]) * 60` → `time_mean`; `increase(x[1m])` at 1 m → `sum`, at 15 s → `time_mean`;
  `a / b` → `ratio`; `x offset 1h` → `time_mean` (not a plain selector for `is_selector`,
  `promql.py:46`, so it is fetched through the rollup path); `@`; subqueries; quoted `/` and `#`
  inside label values never change the kind.
- **Merge arithmetic** (`test_resample.py`): the §1 worked numbers (12.5 → 15; 187.5 → 225 per
  tile, total 900); the (1 + 2p) fixture (spill/neighbour pairs) reads `I` under `sum`; gaps are
  excluded, never zero; NaN handling unchanged; envelope unchanged for every kind.
- **Summary** (`test_summary.py`): output shape per kind; payload size with 5 series ≤ today + 200
  bytes; `total_lower_bound` with a gap; `mean: null` + `mean_unavailable` for ratio/unknown.
- **Rewrite** (`test_rewindow.py`): golden strings for template expansion, tile → S, widen,
  untouched windows ≥ S, `offset`, `@`; `profile_target` unchanged by the move.
- **Re-query path** (`test_lod_requery.py`): a fake source (`tests/unit/fakes.py`) counting
  calls: first `panel_data` → `pending` + envelope; background completes → socket frame →
  second call `requeried` with zero source calls (cache); a different width mapping to the same
  `S` hits the cache; a failing source → `failed` + `requery_failed`, no line; code output →
  `cannot_combine`, no fetch.
- **Callers**: indexed baselines, `hourly_means`, `_prepare` coarsening (events scale × kind),
  fleet coarsening, references and limit lines each get one test pinning the kind they use.
- **UI** (vitest): `describeShown` per kind; tooltip total for `sum`; pending/failed badges.
  Playwright e2e on the fixture source: an increase panel keeps its y-values when the panel is
  resized; a ratio panel shows the pending badge, then the line.
- **Integration** (VM container): for a jittered counter, local `sum` of 15 s tiles equals
  `increase(x[1m])` at 1 m (VM, exact); local `time_mean` of `rate(x[1m])` equals the VM rollup at
  the coarser step within 1e-9 relative when every bucket is fully evaluated; a ratio re-query
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
- **Envelope-only views** are new in the chart; they must not read as "no data" (hatching is
  reserved for gaps).

## 11. Open questions for the user

1. **F1 shortcut.** For step-invariant derived expressions (no template, no tile, nothing to
   widen) the source's coarse value equals the local time mean. OK to compute it locally (labelled
   "mean over each bucket of the plotted value") instead of re-querying, keeping the re-query for
   expressions the rewrite changes? `ratio` keeps `cannot_combine` either way.
2. **Sliding count windows.** `increase(x[5m])` at a 15 s step is classified `time_mean` (per 5 m),
   not `sum`: summing overlapping windows counts each event 20 times. Confirm the agreed
   "increase → sum" means tiles only.
3. **Increase on zoom**: per original tile (recommended, §6) vs per second vs sum per displayed
   bucket.
4. **Code outputs**: let `Lineage` declare `merge` (`time_mean`/`sum`/`max`/`min`/`sample_mean`),
   or always `cannot_combine` beyond native resolution?
5. **Principle 10 wording**: "means and ratios merge only with their counts". A time mean merges
   with its covered time, not its sample count. Amend P10 to "with their weights (samples for a
   sample mean, covered time for a time mean)" before implementing?
6. **Re-query views as evidence**: keep them uncitable views (recommended), or let "keep this
   view" materialise one as a dataset?
