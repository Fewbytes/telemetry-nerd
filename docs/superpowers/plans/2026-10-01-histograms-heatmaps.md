# Histograms and Heatmaps: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking. Beads: `telemetry-nerd-4ok.3` (distribution datasets), `telemetry-nerd-4ok.5` (heatmap mark), `telemetry-nerd-4ok.4` (histogram chart, compare windows, ECDF). Epic M4 is `telemetry-nerd-4ok`. Child beads are listed at the end.

**Goal:** A latency histogram (classic `le`, VictoriaMetrics `vmrange`, or Prometheus native) becomes a **distribution dataset** of per-step bucket counts. It renders as a **heatmap** (time × value bucket, colour = count), and any time selection opens a **histogram / ECDF** of that window, which can be compared with a baseline window. All of this comes from bucket counts. Quantiles are never an input.

**Order: vertical slice first.**
1. Phase A: classic `_bucket` histogram → distribution dataset → heatmap in the UI, end to end (Tasks 1–8).
2. Phase B: native histograms and vmrange (Tasks 9–11).
3. Phase C: histogram chart with brush, compare windows and ECDF (Tasks 12–15).
4. Phase D: polish (Tasks 16–17).

**Architecture:**
- **One query shape for all three forms.** `Source.fetch_histogram(selector, by, rng, step)` runs a single query:
  ```
  sum by (le, vmrange, <by…>) (increase(<selector>[<step>]))
  ```
  - Classic series keep `le`.
  - VictoriaMetrics series keep `vmrange`.
  - Native histograms have neither label and come back in a `histograms` field.
- **Parsing.** A pure parser (`analysis/histogram.py: from_matrix`) turns the matrix into a `DistResult`:
  - `rows`: non-zero `(ts_ms, series_id, bucket_lo, bucket_hi, count)`.
  - `columns`: `(ts_ms, series_id, n)`, one per step that has data. A missing column means no data; `n = 0` means zero observations.
  - The `BucketScheme` (classic `le` set, native schema, or vmrange 18 per decade).
  - Conversion caveats.
- **Storage.** `DatasetStore` keeps distribution datasets in two new DuckDB tables. `DatasetMeta.representation = "distribution"` carries the scheme, `n_min`, and the histogram's `{selector, by}`.
- **What Claude sees.** The summary gives n, columns (with data / zero / missing / low-n), the bucket scheme (the resolution limit), and the *bucket* holding p50/p90/p99 where n is big enough. No interpolated quantiles.
- **LOD.** Counts are additive, so LOD can sum columns in time and merge adjacent value buckets into unions of whole source buckets (never finer than the source).
- **UI.** Custom canvas renderers: `HeatmapPlot` (log-y, viridis, hatched "no data", dimmed low-n columns, tooltip, brush) and `DistributionPlot` (source-bucket bars, density for unequal widths, ECDF exact at edges with boxes inside buckets, max |ΔF| at edges).
- **Brush.** A brush on a heatmap, or on a percentile panel whose expression reveals its histogram, offers "Distribution here". That creates a histogram panel comparing the selection with the preceding window.

**Tech Stack:** Python ≥3.12, uv, polars, pyarrow, duckdb, pydantic v2, httpx, pytest + respx + hypothesis. UI: Svelte 5 + TypeScript, Canvas2D, vitest.

**Spec:** `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md`:
- §1.2 principles 3–4 (correct over conventional; every number carries its uncertainty and aggregation);
- §3.2 distribution rows and representation;
- §4.1 `fetch_histogram`;
- §5.1 distribution ops ("Histograms first", ECDF exact at bucket edges, quantile with bucket-edge bounds);
- §6.1 marks `heatmap`, `ecdf`, auto-charting histogram → heatmap;
- §6.3 perceptually uniform colormaps, series budget;
- §6.5 custom canvas renderers, pixel-aware LOD, render budget.

Prior plan: `docs/superpowers/plans/2026-10-01-honest-percentiles.md` (`n_min` rule, `exprkind`, JSON hygiene).

---

## Empirical findings (recorded 2026-10-01; build fixtures from these)

All Play requests were sent with a descriptive User-Agent, and each was a narrow query.

**(a) Classic `le` buckets, query_range.**
- Grafana Play: `sum by (le) (increase(http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice"}[5m]))`, step 300.
- Plain `values` per `le` series.
- **Series arrive sorted lexicographically by `le`** (`"10"` before `"2.5"`), so sort numerically.
- Counts are **fractional** because Prometheus `increase()` extrapolates.
- Every `le` series is present at every step, including zeros.

```json
{"status":"success","data":{"resultType":"matrix","result":[
 {"metric":{"le":"+Inf"},"values":[[1790865300,"267.5"],[1790865600,"253.75"],[1790865900,"258.75"]]},
 {"metric":{"le":"0.005"},"values":[[1790865300,"241.25"],[1790865600,"213.75"],[1790865900,"227.5"]]},
 {"metric":{"le":"0.01"},"values":[[1790865300,"261.25"],[1790865600,"243.75"],[1790865900,"255"]]},
 {"metric":{"le":"0.025"},"values":[[1790865300,"267.5"],"…"]},
 {"metric":{"le":"10"},"values":["…"]},{"metric":{"le":"2.5"},"values":["…"]}]}}
```

**(b) VictoriaMetrics `vmrange`, query_range** (local dev VM v1.137, `/api/v1/query_range`, step 300).
- `sum by (vmrange) (histogram_over_time(tn_demo_latency_seconds[5m]))` returns per-range counts (not cumulative).
- The data is **sparse**: a range is absent at a step where it had no observations.
- Range strings are `"%.3e...%.3e"`, log-uniform at 18 per decade (ratio 10^(1/18) ≈ 1.136).
- `increase(x_bucket[...])` on VictoriaMetrics/metrics histograms (`x_bucket{vmrange=…}` counters) has the same shape.

```json
{"status":"success","data":{"resultType":"matrix","result":[
 {"metric":{"vmrange":"5.275e-02...5.995e-02"},"values":[[1790865000,"19"]]},
 {"metric":{"vmrange":"5.995e-02...6.813e-02"},"values":[[1790864700,"10"],[1790865000,"38"]]},
 {"metric":{"vmrange":"6.813e-02...7.743e-02"},"values":[[1790864400,"46"],[1790864700,"50"],[1790865000,"3"]]},
 {"metric":{"vmrange":"7.743e-02...8.799e-02"},"values":[[1790864400,"14"]]}]}}
```

`prometheus_buckets(...)` of the same data returns cumulative `le` series, including `+Inf` and a zero row below the lowest range: `{"le":"5.275e-02"} 0,0,0`, `{"le":"5.995e-02"} 0,0,19`, …, `{"le":"+Inf"} 60,60,60`. We parse `vmrange` directly instead: same resolution, no conversion.

**(c) Prometheus native histograms** (Grafana Play `grafanacloud-prom`, Mimir).
- **query_range returns a `histograms` field instead of `values`**: `[[ts, {count, sum, buckets: [[boundary_rule, lo, hi, count], …]}], …]`.
- Instant queries return `histogram` (singular) instead of `value`.
- All numbers are strings.
- Only **non-empty buckets** are listed.
- Boundary rule `0` means `(lo, hi]`. The zero bucket uses rule `3` `[-z, z]`.
- **`count` (= `histogram_count`) can differ from the bucket sum** (8.0008 vs 8.0 below), so n per column must come from `count`.
- Bucket bounds are 2^(i·2^-schema). Play uses schema 3 (×1.0905 per bucket). An aggregate across series with different schemas comes back at the coarsest one (us-east-2 showed ×1.189 = schema 2).
- `sum by (le, vmrange, cloud_region)` works on native histograms; the absent labels are ignored.

```json
{"status":"success","data":{"resultType":"matrix","result":[{"metric":{"cloud_region":"eu-west-1"},
 "histograms":[[1790865300,{"count":"5.208354166666666","sum":"1.7589562378801422","buckets":[
   [0,"0.29730177875068026","0.3242098886627524","2.0833416666666666"],
   [0,"0.3242098886627524","0.35355339059327373","1.0416708333333333"],
   [0,"0.35355339059327373","0.3855527063519852","2.0833416666666666"]]}],
  [1790865600,{"count":"5","sum":"1.8587971999999997","buckets":[[0,"0.29730177875068026","0.3242098886627524","1.25"],"…"]}]]}]}}
```

**Acceptance scenario on Play:** `ap-south-1`, `increase(traces_spanmetrics_latency{service="checkoutservice",span_kind="SPAN_KIND_SERVER",cloud_region="ap-south-1"}[1m])`, 10:00–10:10Z, step 60.
- **Only one column has data**: 10:02Z (ts 1790848920) with `count = 8.0008`.
- Its buckets are (14.67, 16] = 2, (29.34, 32] = 2, (32, 34.90] = 4. So at a 1 m step the spike is 8 slow requests, not "13 split fast vs 10–50 s". The bead's 13 came from a wider window.
- Other minutes have no histogram at all: empty, not zero.

**Fixtures to record** with `sources/replay.py` (Task 11, script `scripts/record_play_histograms.py`, fixed historical windows):
1. Native: `fetch_histogram('traces_spanmetrics_latency{service="checkoutservice",span_kind="SPAN_KIND_SERVER"}', ["cloud_region"], 2026-10-01T09:50Z–10:20Z, 1m)`. Covers the burst and the spike; three regions give small multiples.
2. Native n check: `fetch_values('sum by (cloud_region) (histogram_count(increase(traces_spanmetrics_latency{service="checkoutservice",span_kind="SPAN_KIND_SERVER"}[1m])))', same window, 1m)`.
3. Classic: `fetch_histogram('http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice"}', [], 2026-10-01T12:00Z–13:00Z, 5m)`.
4. Classic n check: `fetch_values('sum(increase(http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice",le="+Inf"}[5m]))', same, 5m)`.

The vmrange and classic VictoriaMetrics cases come from testcontainers integration tests with synthetic pushes (exact ground truth), not from recordings.

---

## Design decisions (recommendations)

1. **Storage: new tables, not `dataset_rows`.**
   - Tables: `dist_rows(dataset_id, ts_ms, series_id, bucket_lo DOUBLE, bucket_hi DOUBLE, count DOUBLE)` and `dist_columns(dataset_id, ts_ms, series_id, n DOUBLE)`.
   - Only non-zero buckets are stored. Column presence encodes **empty (no data) vs zero (n = 0)**, and within a present column an absent bucket is zero.
   - `count` is DOUBLE: Prometheus `increase()` extrapolates, and rounding per bucket would break additivity. Fractional counts raise an `estimated_counts` caveat.
   - ±Inf edges are stored as DOUBLE infinities. In JSON they become `null`: `bucket_lo: null` means −Inf and `bucket_hi: null` means +Inf (documented in `api.ts`). In Claude summaries they appear as the strings `"-Inf"` / `"+Inf"`.
2. **Representation plumbing.**
   - `DatasetMeta` gains `scheme`, `histogram` (`{selector, by}`) and `source_caveats`, with `representation="distribution"` and `n_min = 20`.
   - `DatasetStore` gains `meta()`, `series_count()`, `put_distribution()` and `get_distribution()`. `get()` refuses distribution datasets so nothing silently reads an empty bucket table.
   - `summarize_distribution` is a separate summary.
   - `panel_data` returns `kind: "time" | "heatmap" | "histogram"`.
   - `charts/spec.py` marks become `line+envelope | heatmap | histogram | ecdf`. `auto_spec(..., representation)` picks `heatmap` for distributions. `validate(..., representations)` refuses lines on distributions and distribution marks on anything else, enforces the 12-facet small-multiple budget, and requires 1–4 windows for `histogram`/`ecdf`.
3. **How Claude asks: a new MCP tool `query_distribution(selector, by, start, end, step, source)`, not auto-detection inside `query`.**
   - The server owns the window (= step) and the form detection, so Claude cannot pick a `rate` window that overlaps steps or wrap buckets in a quantile.
   - `query` keeps its meaning and gains a clear error when an expression returns native histograms. Today that path fails with "malformed series".
   - Quantile datasets learn their histogram (`exprkind.histogram_source`) so the UI can offer "Distribution here".
4. **Counts per step** come from `increase(sel[step])` evaluated at each step end.
   - Column `ts` covers `(ts − step, ts]`, matching `rollup`, so columns tile time.
   - Step floor = 2 × scrape resolution, so `increase` sees at least two samples. Auto step targets about 300 columns (heatmap columns ≥ 2 px).
   - VictoriaMetrics uses the sample before each window, so its counts are exact. Prometheus misses the delta between windows and extrapolates instead; its totals are approximately right and are flagged `estimated_counts`.
5. **`le`-cumulative → per-bucket conversion.**
   - Sort `le` numerically. Per bucket = difference of adjacent cumulatives. The first bucket is (−Inf, e₀]; the last is (e_last, +Inf].
   - Non-monotonic cumulatives (independent extrapolation or a reset seen by some `le` series only) carry the running maximum forward, as `histogram_quantile` does, and raise `non_monotonic`. Float noise below 1e-9 relative is ignored.
   - With no `+Inf`, n is a lower bound: `missing_inf`.
   - Counter resets are handled by `increase()` per series on the server.
6. **Bucket scheme (the honest resolution).**
   - Classic: the **intersection** of `le` sets across series, so value-LOD edges exist for every series.
   - Native: the coarsest schema derived from the bucket bounds, after checking the bounds lie on the 2^(i·2^-s) grid. Otherwise it is `custom` (NHCB).
   - vmrange: 18 per decade.
   - Value LOD only ever merges whole source buckets: every m-th classic edge, a lower native schema (powers of two, exact), or groups of m vmranges.
7. **Caps.**
   - Fetch: existing `max_series` (500) and `max_points` (2 M non-zero rows), ≤ 11,000 steps per query, ≤ 50,000 columns per dataset.
   - Heatmap: ≥ 2 px per column and ≥ 4 px per value row (time-sum and value-merge LOD), ≤ 12 facets, so cells ≤ area/8.
   - Render report: `points = cells`, with a new optional `height_px`. Budget exceeded if > 100 ms or cells > width × height / 8.
   - Histogram bars: ≥ 3 px per bar via value merge.
8. **Heatmap renderer.**
   - Canvas per facet (260 px for one series, 140 px each for 2–12).
   - Value axis is log when positive edges span ≥ 2 decades (spec §6.2), otherwise linear. Open buckets get 10 px strips ("≤ e₀", "> e_last").
   - Colour is viridis (cividis toggle). Count mode uses log1p(count)/log1p(max). Density mode uses the column's share, linear.
   - Zero cells are not painted (background). Columns with no data are hatched. Low-n columns (0 < n < 20) are dimmed to 45% with an orange tick on the time axis.
   - Tooltip: time range, bucket edges with unit, count, n, share. A legend gradient shows the counts.
   - Optional per-column quantile overlay (Task 16) draws the bucket containing q, only where n ≥ min_samples(q), and never an interpolated line.
9. **Histogram chart.**
   - Windows snap outward to whole columns. Each window = Σ counts per bucket and Σ n.
   - Bars are the source buckets. Y modes: count (one window only), share, or density = share / log₁₀(hi/lo), which is fair for unequal classic buckets on log-x. With ≥ 2 windows count mode is disabled, since n differs between windows.
   - ECDF is exact at bucket edges, F(hi) = cum/total. Inside a bucket it is drawn as a box [F(lo), F(hi)] and never interpolated. "max |ΔF| at bucket edges" (a lower bound on the KS distance) is computed only where both ECDFs are exact.
   - n and columns are always in the legend.
10. **Low-n threshold for distributions:** `DIST_N_MIN = min_samples(0.5) = 20`. Below about 20 observations a column's shape is noise. This reuses the 4ok.1 rule and the `low_count` caveat key.
11. **No SeriesCache for distributions in MVP.** Each distribution query hits the source once, through the source's politeness gate. Caching is a follow-up bead.

## Global Constraints

- Work directly on `master`; another stream (3fs.5 highlights) is committing there too. Keep `Panel.svelte` edits minimal and additive, and run `git pull --rebase`-free (local only) checks before each commit (`git status`).
- Commit after every task. End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Python via `uv`, tasks via `just`, search with `rg`/`fd`. After every task run `just lint` and `just test`. UI tasks also run `just ui-test`, `just ui-check` (the 2 pre-existing esrap type errors are known; nothing new) and `just ui-build`.
- **Do not run `just e2e`** (it wipes the dev VictoriaMetrics volume). E2E specs are written for the user to run.
- **Never aggregate percentiles.** Distribution counts come from `increase()` over buckets, never from quantiles. Summed counts are additive; percentiles are not.
- **Bins are source buckets.** Never interpolate within a bucket and never split one. Quantiles are reported as the containing bucket `[lo, hi]`.
- JSON never contains NaN/Inf (`model/jsonsafe.py`, `JSONResponse`). Infinite bucket edges are `null` in panel data and `"±Inf"` strings in summaries.
- Existing `query`/`show`/`panel_data` behaviour for non-distribution datasets must not change. The only additions are `kind: "time"` in `panel_data` and the native-histogram error message.
- Polite to Grafana Play: the Task 11 recording only; one request at a time (preset `max_concurrency=1`, `min_interval_ms=1000`).

## File Structure

```
src/telemetry_nerd/model/distribution.py      # NEW DIST_SCHEMA, COLUMN_SCHEMA, BucketScheme, DistResult, DIST_N_MIN
src/telemetry_nerd/analysis/histogram.py      # NEW histogram_expr, cumulative_to_buckets, parse_vmrange, native_buckets, native_schema, from_matrix
src/telemetry_nerd/analysis/distlod.py        # NEW rebucket_time, merge_values, window_histogram, LOD constants
src/telemetry_nerd/analysis/exprkind.py       # MOD histogram_source (Task 12)
src/telemetry_nerd/sources/base.py            # MOD Source.fetch_histogram
src/telemetry_nerd/sources/promql.py          # MOD fetch_histogram; native-histogram guard in fetch/fetch_values
src/telemetry_nerd/sources/presets.py         # MOD PLAY preset (Task 11)
src/telemetry_nerd/datasets/db.py             # MOD dist_rows, dist_columns
src/telemetry_nerd/datasets/store.py          # MOD DatasetMeta fields; meta, series_count, put_distribution, get_distribution
src/telemetry_nerd/core/summary.py            # MOD _base_caveats; summarize_distribution
src/telemetry_nerd/core/service.py            # MOD query_distribution, show(mark, windows), panel_data kinds, distribution_panel
src/telemetry_nerd/charts/spec.py             # MOD marks, Window, auto_spec(representation), validate(representations)
src/telemetry_nerd/api/app.py                 # MOD /api/query-distribution, /api/panels/{id}/distribution, heatmap render budget
src/telemetry_nerd/mcp/server.py              # MOD query_distribution tool, show(mark, windows), INSTRUCTIONS
src/telemetry_nerd/devtools/synthetic.py      # MOD classic latency histogram in demo_text
scripts/record_play_histograms.py             # NEW (Task 11)
ui/src/lib/api.ts                             # MOD PanelData union, HeatSeries, WindowHist, DatasetMeta fields
ui/src/lib/panelNotes.ts                      # MOD distribution caveats, describeShown
ui/src/chart/colormap.ts                      # NEW viridis/cividis + luminance
ui/src/chart/heatmap.ts                       # NEW valueAxis, layoutHeatmap, hitTest, timeAt
ui/src/chart/axis.ts                          # NEW valueTicks, fmtValue
ui/src/chart/distribution.ts                  # NEW bars, ecdf, maxEcdfGapAtEdges (Task 14)
ui/src/components/HeatmapPlot.svelte          # NEW canvas heatmap facet
ui/src/components/DistributionPlot.svelte     # NEW histogram/ECDF canvas (Task 15)
ui/src/components/SelectionMenu.svelte        # MOD "Distribution here"
ui/src/Panel.svelte                           # MOD branch on data.kind
tests/unit/test_histogram.py                  # NEW parsers/converters
tests/unit/test_distlod.py                    # NEW LOD + windows (+ hypothesis additivity)
tests/unit/test_service_distribution.py       # NEW service paths
tests/unit/test_play_fixtures.py              # NEW recorded Play replay (Task 11)
tests/unit/{test_promql,test_dataset_store,test_summary,test_chart_spec,test_api,test_exprkind,test_synthetic}.py  # MOD
tests/unit/fakes.py                           # MOD FakeSource.fetch_histogram
tests/integration/test_distribution_vm.py     # NEW classic + vmrange through VictoriaMetrics
tests/fixtures/play/                          # NEW recorded fixtures
ui/src/chart/{colormap,heatmap,axis,distribution}.test.ts  # NEW
ui/e2e/distribution.spec.ts                   # NEW (not run by the implementer)
```

## Scope notes

- `fraction_over`, `compare_dist` (KS/Wasserstein with bootstrap CI), marginal histograms (4ok.6) and spectrograms (4ok.8) are out of scope. The ECDF edge gap is a descriptive lower bound, not a test.
- `histogram_over_time` of a *gauge* (VictoriaMetrics only) is an optional follow-up bead (`of="gauge"`). It is not needed for the acceptance criteria.
- Native negative buckets are parsed and stored, but the log axis puts them in the low strip and value LOD leaves them unmerged.
- Distribution datasets bypass `SeriesCache` (follow-up bead).
- Zoom-in refetch is not implemented (unchanged from today).

---

## Phase A: vertical slice (classic → dataset → heatmap)

### Task 1: Distribution model and classic `le` conversion (pure)

**Files:**
- Create: `src/telemetry_nerd/model/distribution.py`, `src/telemetry_nerd/analysis/histogram.py`
- Test: `tests/unit/test_histogram.py`

**Interfaces (produces):**
- `DIST_SCHEMA`, `COLUMN_SCHEMA`, `DIST_N_MIN = 20`, `VM_PER_DECADE = 18`
- `BucketScheme(kind, edges=(), schema=None, per_decade=None)`, with `.growth`, `.describe()`, `.to_dict()` and `BucketScheme.from_dict()`
- `DistResult(rows, columns, series, scheme, expr="", caveats=())`
- `histogram_expr(selector, by, step_ms) -> str`
- `cumulative_to_buckets(cum) -> (buckets, n, problems) | None`
- `from_matrix(source, result, expr="") -> DistResult`. Classic only in this task; native and vmrange raise `ValueError("… not supported yet")`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_histogram.py
import math

import pytest

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.analysis.histogram import cumulative_to_buckets, from_matrix, histogram_expr
from telemetry_nerd.model.distribution import DIST_N_MIN, BucketScheme
from telemetry_nerd.model.series import series_id

INF = math.inf


def test_dist_n_min_is_the_median_rule():
    assert DIST_N_MIN == min_samples(0.5) == 20


def test_histogram_expr_groups_by_both_bucket_labels():
    assert histogram_expr('lat_bucket{job="a"}', ["region"], 60_000) == (
        'sum by (le, vmrange, region) (increase(lat_bucket{job="a"}[1m]))'
    )


@pytest.mark.parametrize("label", ["le", "vmrange", "a-b", ""])
def test_histogram_expr_rejects_bad_labels(label):
    with pytest.raises(ValueError):
        histogram_expr("x_bucket", [label], 60_000)


def test_cumulative_to_per_bucket_with_inf_bucket():
    buckets, n, problems = cumulative_to_buckets({0.1: 9.0, 1.0: 9.9, 10.0: 10.0, INF: 10.0})
    assert n == 10.0 and problems == set()
    assert buckets == [
        (-INF, 0.1, 9.0),
        (0.1, 1.0, pytest.approx(0.9)),
        (1.0, 10.0, pytest.approx(0.1)),
    ]


def test_overflow_bucket_is_kept():
    buckets, n, _ = cumulative_to_buckets({1.0: 3.0, INF: 5.0})
    assert buckets[-1] == (1.0, INF, 2.0) and n == 5.0


def test_zero_buckets_are_dropped_but_n_is_kept():
    assert cumulative_to_buckets({1.0: 0.0, INF: 0.0}) == ([], 0.0, set())


def test_non_monotonic_carries_running_max_and_flags():
    buckets, n, problems = cumulative_to_buckets({0.1: 5.0, 1.0: 4.0, INF: 6.0})
    assert problems == {"non_monotonic"}
    assert buckets == [(-INF, 0.1, 5.0), (1.0, INF, 1.0)]
    assert n == 6.0


def test_float_noise_is_not_non_monotonic():
    assert cumulative_to_buckets({0.1: 5.0, 1.0: 5.0 - 1e-12, INF: 5.0})[2] == set()


def test_missing_inf_bucket_flags_lower_bound():
    _, n, problems = cumulative_to_buckets({0.1: 1.0, 1.0: 2.0})
    assert n == 2.0 and problems == {"missing_inf"}


def test_all_nan_column_is_absent():
    assert cumulative_to_buckets({0.1: float("nan"), INF: None}) is None


# shape of Grafana Play query_range, sum by (le) (increase(..._bucket[5m])), trimmed;
# note the lexicographic le order the server returns
CLASSIC = [
    {"metric": {"le": "+Inf"}, "values": [[1790865300, "267.5"], [1790865600, "253.75"]]},
    {"metric": {"le": "0.005"}, "values": [[1790865300, "241.25"], [1790865600, "213.75"]]},
    {"metric": {"le": "0.01"}, "values": [[1790865300, "261.25"], [1790865600, "243.75"]]},
    {"metric": {"le": "10"}, "values": [[1790865300, "267.5"], [1790865600, "253.75"]]},
    {"metric": {"le": "2.5"}, "values": [[1790865300, "267.5"], [1790865600, "253.75"]]},
]


def test_classic_matrix_to_distribution():
    d = from_matrix("play", CLASSIC, expr="e")
    assert d.expr == "e"
    assert d.scheme == BucketScheme("classic", edges=(0.005, 0.01, 2.5, 10.0))
    assert [(c["ts_ms"], c["n"]) for c in d.columns.to_pylist()] == [
        (1790865300000, 267.5),
        (1790865600000, 253.75),
    ]
    first = [
        (r["bucket_lo"], r["bucket_hi"], r["count"])
        for r in d.rows.to_pylist()
        if r["ts_ms"] == 1790865300000
    ]
    assert first == [(-INF, 0.005, 241.25), (0.005, 0.01, 20.0), (0.01, 2.5, 6.25)]
    assert {r["series_id"] for r in d.rows.to_pylist()} == {series_id("play", {})}
    assert "estimated_counts" in d.caveats


def test_series_identity_drops_le_and_vmrange():
    d = from_matrix(
        "s",
        [
            {"metric": {"le": "+Inf", "region": "a"}, "values": [[1, "2"]]},
            {"metric": {"le": "1", "region": "a"}, "values": [[1, "1"]]},
        ],
    )
    assert d.series.to_pylist() == [
        {"series_id": series_id("s", {"region": "a"}), "labels": '{"region":"a"}'}
    ]
    assert d.caveats == ()


def test_classic_scheme_is_the_le_intersection_across_series():
    d = from_matrix(
        "s",
        [
            {"metric": {"le": "1", "r": "a"}, "values": [[1, "1"]]},
            {"metric": {"le": "2", "r": "a"}, "values": [[1, "1"]]},
            {"metric": {"le": "+Inf", "r": "a"}, "values": [[1, "1"]]},
            {"metric": {"le": "1", "r": "b"}, "values": [[1, "1"]]},
            {"metric": {"le": "+Inf", "r": "b"}, "values": [[1, "1"]]},
        ],
    )
    assert d.scheme.edges == (1.0,)


def test_plain_series_is_not_a_histogram():
    with pytest.raises(ValueError, match="not a histogram"):
        from_matrix("s", [{"metric": {"job": "x"}, "values": [[1, "2"]]}])


def test_scheme_round_trip_and_description():
    s = BucketScheme("classic", edges=(0.1, 1.0, 10.0))
    assert BucketScheme.from_dict(s.to_dict()) == s
    assert s.describe() == "classic le buckets: 0.1, 1, 10"
    assert BucketScheme.from_dict(None).kind == "none"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_histogram.py -q`
Expected: FAIL with `ModuleNotFoundError: telemetry_nerd.model.distribution`

- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/model/distribution.py
"""Distribution datasets (spec §3.2): per-step histogram counts, never quantiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pyarrow as pa

from telemetry_nerd.model.series import SERIES_SCHEMA

DIST_SCHEMA = pa.schema(
    [
        ("ts_ms", pa.int64()),  # end of the step window (ts - step, ts]
        ("series_id", pa.string()),
        ("bucket_lo", pa.float64()),  # -inf allowed (classic first bucket)
        ("bucket_hi", pa.float64()),  # +inf allowed (overflow bucket)
        ("count", pa.float64()),  # increase() over one step; fractional when extrapolated
    ]
)
COLUMN_SCHEMA = pa.schema(
    [("ts_ms", pa.int64()), ("series_id", pa.string()), ("n", pa.float64())]
)

# min_samples(0.5): with fewer observations a column's shape is noise (4ok.1 rule)
DIST_N_MIN = 20
VM_PER_DECADE = 18  # VictoriaMetrics vmrange buckets: log-uniform, 18 per decade
_LIST_EDGES = 16

SchemeKind = Literal["none", "classic", "native", "vmrange", "custom"]


@dataclass(frozen=True)
class BucketScheme:
    kind: SchemeKind
    edges: tuple[float, ...] = ()  # classic/custom: finite edges, ascending
    schema: int | None = None  # native exponential schema: growth 2^(2^-schema)
    per_decade: int | None = None  # vmrange

    @property
    def growth(self) -> float | None:
        if self.schema is not None:
            return 2.0 ** (2.0**-self.schema)
        if self.per_decade:
            return 10.0 ** (1 / self.per_decade)
        return None

    def describe(self) -> str:
        if self.kind == "native" and self.schema is not None:
            return f"native exponential, schema {self.schema} (each bucket x{self.growth:.3g})"
        if self.kind == "vmrange":
            return (
                f"VictoriaMetrics vmrange, {self.per_decade} per decade "
                f"(each bucket x{self.growth:.3g})"
            )
        if self.kind in ("classic", "custom") and self.edges:
            name = "classic le" if self.kind == "classic" else "custom"
            if len(self.edges) <= _LIST_EDGES:
                return f"{name} buckets: {', '.join(f'{e:g}' for e in self.edges)}"
            return f"{name} buckets: {len(self.edges)} edges {self.edges[0]:g}..{self.edges[-1]:g}"
        return "no buckets"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "edges": list(self.edges),
            "schema": self.schema,
            "per_decade": self.per_decade,
            "description": self.describe(),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> BucketScheme:
        if not d:
            return cls("none")
        return cls(d["kind"], tuple(d.get("edges") or ()), d.get("schema"), d.get("per_decade"))


@dataclass(frozen=True)
class DistResult:
    rows: pa.Table  # DIST_SCHEMA; only buckets with count > 0
    columns: pa.Table  # COLUMN_SCHEMA; one row per (series, step) with data; n may be 0
    series: pa.Table  # SERIES_SCHEMA
    scheme: BucketScheme
    expr: str = ""
    caveats: tuple[str, ...] = ()
```

```python
# src/telemetry_nerd/analysis/histogram.py
"""Histogram query results -> per-step distribution rows (spec §3.2, §4.1).

Counts come from increase() over a window equal to the step, so columns tile time and
are additive: summing over time or over adjacent buckets is valid. Quantiles are never
an input here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

import pyarrow as pa

from telemetry_nerd.model.distribution import (
    COLUMN_SCHEMA,
    DIST_SCHEMA,
    BucketScheme,
    DistResult,
)
from telemetry_nerd.model.series import SERIES_SCHEMA, labels_json, series_id
from telemetry_nerd.model.time import format_duration

GROUPING = ("le", "vmrange")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_TOL = 1e-9

Bucket = tuple[float, float, float]  # (lo, hi, count)


def histogram_expr(selector: str, by: Sequence[str], step_ms: int) -> str:
    """One expression for all forms: classic keeps `le`, VictoriaMetrics keeps `vmrange`,
    native histograms have neither and come back in a `histograms` field."""
    for label in by:
        if not _LABEL.match(label) or label in GROUPING:
            raise ValueError(f"invalid group-by label {label!r}")
    labels = ", ".join([*GROUPING, *by])
    return f"sum by ({labels}) (increase({selector.strip()}[{format_duration(step_ms)}]))"


def cumulative_to_buckets(
    cum: dict[float, float | None],
) -> tuple[list[Bucket], float, set[str]] | None:
    """One series at one step: {le: cumulative count} -> non-zero buckets, n, problems.

    Cumulative counts must not decrease with le, but they can (each le series is
    extrapolated and reset-corrected independently by increase()). Like
    histogram_quantile we carry the running maximum forward and report
    `non_monotonic`. Without a +Inf bucket, n is a lower bound: `missing_inf`.
    """
    points = sorted((le, v) for le, v in cum.items() if v is not None and math.isfinite(v))
    if not points:
        return None
    problems: set[str] = set()
    if points[-1][0] != math.inf:
        problems.add("missing_inf")
    out: list[Bucket] = []
    prev_le, prev_c = -math.inf, 0.0
    for le, c in points:
        if c < prev_c:
            if prev_c - c > _TOL * max(1.0, prev_c):
                problems.add("non_monotonic")
            c = prev_c
        if c > prev_c:
            out.append((prev_le, le, c - prev_c))
        prev_le, prev_c = le, c
    return out, prev_c, problems


def _ts(value: object) -> int:
    return round(float(value) * 1000)  # type: ignore[arg-type]


def _fractional(x: float) -> bool:
    return abs(x - round(x)) > 1e-6


def from_matrix(source: str, result: list[dict], expr: str = "") -> DistResult:
    """Parse a query_range matrix of histogram_expr(...). Raises ValueError when the
    result is malformed or not a histogram."""
    labels_by_sid: dict[str, dict[str, str]] = {}
    classic: dict[tuple[str, int], dict[float, float | None]] = {}
    les_by_sid: dict[str, set[float]] = {}
    cols: dict[tuple[str, int], float] = {}
    rows: list[tuple[int, str, float, float, float]] = []
    forms: set[str] = set()
    pairs: set[tuple[float, float]] = set()
    for item in result:
        try:
            labels = dict(item["metric"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"malformed series {item!r} in query result") from e
        labels.pop("__name__", None)
        le, vmrange = labels.pop("le", None), labels.pop("vmrange", None)
        sid = series_id(source, labels)
        labels_by_sid[sid] = labels
        if "histograms" in item:
            raise ValueError("native histograms are not supported yet")  # Task 9
        for sample in item.get("values") or []:
            t, x = _ts(sample[0]), float(sample[1])
            if le is not None:
                forms.add("classic")
                edge = float(le)
                les_by_sid.setdefault(sid, set()).add(edge)
                classic.setdefault((sid, t), {})[edge] = x
            elif vmrange is not None:
                raise ValueError("vmrange histograms are not supported yet")  # Task 10
            else:
                raise ValueError(
                    "not a histogram: a series has neither an le nor a vmrange label"
                )
    caveats: set[str] = set()
    for (sid, t), cum in classic.items():
        conv = cumulative_to_buckets(cum)
        if conv is None:
            continue
        buckets, n, problems = conv
        caveats |= problems
        cols[(sid, t)] = n
        rows.extend((t, sid, lo, hi, c) for lo, hi, c in buckets)
    if len(forms) > 1:
        raise ValueError(f"selector matched several histogram forms: {', '.join(sorted(forms))}")
    if any(_fractional(n) for n in cols.values()) or any(_fractional(r[4]) for r in rows):
        caveats.add("estimated_counts")
    return _tables(rows, cols, labels_by_sid, _scheme(forms, les_by_sid, pairs), expr, caveats)


def _scheme(
    forms: set[str], les_by_sid: dict[str, set[float]], pairs: set[tuple[float, float]]
) -> BucketScheme:
    if not forms:
        return BucketScheme("none")
    form = next(iter(forms))
    if form == "classic":
        common = set.intersection(*les_by_sid.values()) if les_by_sid else set()
        return BucketScheme("classic", edges=tuple(sorted(e for e in common if math.isfinite(e))))
    raise ValueError(f"unsupported histogram form {form}")  # Tasks 9-10


def _tables(rows, cols, labels_by_sid, scheme, expr, caveats) -> DistResult:
    rows.sort(key=lambda r: (r[1], r[0], r[2]))
    keys = sorted(cols, key=lambda k: (k[0], k[1]))
    dist = pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "bucket_lo": [r[2] for r in rows],
            "bucket_hi": [r[3] for r in rows],
            "count": [r[4] for r in rows],
        },
        schema=DIST_SCHEMA,
    )
    columns = pa.table(
        {
            "ts_ms": [k[1] for k in keys],
            "series_id": [k[0] for k in keys],
            "n": [cols[k] for k in keys],
        },
        schema=COLUMN_SCHEMA,
    )
    sids = sorted(labels_by_sid)
    series = pa.table(
        {"series_id": sids, "labels": [labels_json(labels_by_sid[s]) for s in sids]},
        schema=SERIES_SCHEMA,
    )
    return DistResult(dist, columns, series, scheme, expr, tuple(sorted(caveats)))
```

- [ ] **Step 4: Run tests and lint**

Run: `uv run pytest tests/unit/test_histogram.py -q && just lint`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/model/distribution.py src/telemetry_nerd/analysis/histogram.py tests/unit/test_histogram.py
git commit -m "feat(analysis): distribution model and classic le to per-bucket counts (4ok.3)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `fetch_histogram` (classic) and a clear error for native histograms in `query`

**Files:**
- Modify: `src/telemetry_nerd/sources/base.py`, `src/telemetry_nerd/sources/promql.py`, `tests/unit/fakes.py`
- Test: `tests/unit/test_promql.py` (append)

**Interfaces:**
- `Source.fetch_histogram(selector: str, by: Sequence[str], rng: TimeRange, step_ms: int) -> DistResult`
- `PromQLSource.fetch_histogram` does the following:
  - refuses non-selectors;
  - runs one `_query_range(histogram_expr(...))`, then `from_matrix`;
  - wraps `ValueError` into `SourceError(hint=_HIST_HINT)`;
  - raises `LimitExceeded` above `max_series` series or `max_points` rows.
- `fetch` / `fetch_values`: a series with a `histograms` field raises `SourceError("…returns native histograms…", hint="…query_distribution…")`.
- `FakeSource(cumulative=None)`: `fetch_histogram` emits per-instance classic cumulatives. The default is `{"0.1": 90, "1": 99, "10": 100, "+Inf": 100}` × (k + 1) for instance `i{k}`. It records `hist_selectors`.

- [ ] **Step 1: Failing tests** (append to `tests/unit/test_promql.py`; add `import math`)

```python
@respx.mock
async def test_fetch_histogram_classic_is_one_query():
    route = respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=matrix(
                [
                    {"metric": {"le": "0.1", "region": "a"}, "values": [[1_700_000_100, "9"]]},
                    {"metric": {"le": "+Inf", "region": "a"}, "values": [[1_700_000_100, "10"]]},
                ]
            ),
        )
    )
    src = PromQLSource("s", BASE, flavor="prometheus")
    d = await src.fetch_histogram('lat_bucket{job="x"}', ["region"], RNG, 60_000)
    assert route.call_count == 1
    assert route.calls.last.request.url.params["query"] == (
        'sum by (le, vmrange, region) (increase(lat_bucket{job="x"}[1m]))'
    )
    assert d.scheme.kind == "classic"
    assert [(r["bucket_lo"], r["bucket_hi"], r["count"]) for r in d.rows.to_pylist()] == [
        (-math.inf, 0.1, 9.0),
        (0.1, math.inf, 1.0),
    ]
    assert d.columns.to_pylist() == [
        {"ts_ms": 1_700_000_100_000, "series_id": series_id("s", {"region": "a"}), "n": 10.0}
    ]


async def test_fetch_histogram_refuses_expressions():
    with pytest.raises(SourceError, match="selector"):
        await PromQLSource("s", BASE).fetch_histogram("rate(x_bucket[5m])", [], RNG, 60_000)


@respx.mock
async def test_fetch_histogram_on_a_plain_series_explains():
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200, json=matrix([{"metric": {"job": "x"}, "values": [[1_700_000_100, "3"]]}])
        )
    )
    with pytest.raises(SourceError, match="not a histogram") as e:
        await PromQLSource("s", BASE).fetch_histogram("x_total", [], RNG, 60_000)
    assert "_bucket" in (e.value.hint or "")


@respx.mock
async def test_fetch_histogram_series_limit():
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=matrix(
                [
                    {"metric": {"le": "+Inf", "r": r}, "values": [[1_700_000_100, "1"]]}
                    for r in ("a", "b")
                ]
            ),
        )
    )
    src = PromQLSource("s", BASE, limits=Limits(max_series=1))
    with pytest.raises(LimitExceeded):
        await src.fetch_histogram("x_bucket", ["r"], RNG, 60_000)


@respx.mock
async def test_query_on_native_histograms_explains_instead_of_malformed():
    native = {"metric": {}, "histograms": [[1_700_000_100, {"count": "1", "sum": "1", "buckets": []}]]}
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix([native])))
    src = PromQLSource("s", BASE, flavor="prometheus")
    for call in (src.fetch_values, src.fetch):
        with pytest.raises(SourceError, match="native histograms") as e:
            await call("sum(rate(lat[5m]))", RNG, 60_000)
        assert "query_distribution" in (e.value.hint or "")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_promql.py -q`
Expected: FAIL with `AttributeError: fetch_histogram`; the native test fails with "malformed series".

- [ ] **Step 3: Implement**

`base.py`: add `from collections.abc import Sequence` and `from telemetry_nerd.model.distribution import DistResult`. Add to the `Source` protocol:

```python
    async def fetch_histogram(
        self, selector: str, by: Sequence[str], rng: TimeRange, step_ms: int
    ) -> DistResult: ...
```

`promql.py`: add imports (`from collections.abc import Sequence`; `from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr`; `from telemetry_nerd.model.distribution import DistResult`) and these module helpers:

```python
_HIST_HINT = (
    "select the histogram itself: x_bucket{...} for classic (le) or VictoriaMetrics "
    "(vmrange) histograms, or the native histogram metric x{...}; group with `by`"
)


def _native_histograms() -> SourceError:
    return SourceError(
        "the expression returns native histograms, not numbers",
        hint=(
            "use query_distribution(selector=...) for the distribution, or wrap it in "
            "histogram_count / histogram_sum / histogram_fraction / histogram_quantile"
        ),
    )
```

In both `fetch` and `fetch_values`, inside `for item in result:` and before `samples = list(item["values"])`, add:

```python
                if isinstance(item, dict) and "histograms" in item:
                    raise _native_histograms()
```

New method:

```python
    async def fetch_histogram(
        self, selector: str, by: Sequence[str], rng: TimeRange, step_ms: int
    ) -> DistResult:
        """Per-step bucket counts: increase() over a window equal to the step (spec §4.1)."""
        if not is_selector(selector):
            raise SourceError(f"not a metric selector: {selector!r}", hint=_HIST_HINT)
        steps = (rng.end_ms - rng.start_ms) // step_ms + 1
        if steps > MAX_STEPS_PER_QUERY:
            raise LimitExceeded(
                f"{steps} steps exceeds {MAX_STEPS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        try:
            expr = histogram_expr(selector, by, step_ms)
        except ValueError as e:
            raise SourceError(str(e), hint="`by` takes plain label names other than le/vmrange") from e
        result = await self._query_range(expr, rng, step_ms)
        try:
            dist = from_matrix(self.name, result, expr=expr)
        except (ValueError, TypeError, IndexError, KeyError) as e:
            raise SourceError(str(e), hint=_HIST_HINT) from e
        if dist.series.num_rows > self.limits.max_series:
            raise LimitExceeded(
                f"histogram has {dist.series.num_rows} series (limit {self.limits.max_series})",
                hint="group by fewer labels (by=[...]) or narrow the selector",
            )
        if dist.rows.num_rows > self.limits.max_points:
            raise LimitExceeded(
                f"histogram has {dist.rows.num_rows} non-empty cells (limit {self.limits.max_points})",
                hint="use a coarser step or a shorter range",
            )
        return dist
```

`tests/unit/fakes.py`: add the imports `from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr`. Extend `__init__` with `cumulative=None`:

```python
        self.cumulative = cumulative or {"0.1": 90.0, "1": 99.0, "10": 100.0, "+Inf": 100.0}
        self.hist_selectors: list[str] = []

    async def fetch_histogram(self, selector, by, rng, step_ms):
        self.calls += 1
        self.hist_selectors.append(selector)
        ts = range(rng.start_ms, rng.end_ms + 1, step_ms)
        result = [
            {
                "metric": {"instance": f"i{k}", "le": le},
                "values": [[t / 1000, str(c * (k + 1))] for t in ts],
            }
            for k in range(self.n_series)
            for le, c in self.cumulative.items()
        ]
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))
```

- [ ] **Step 4: Run** `just test && just lint`. Expected: PASS.
- [ ] **Step 5: Commit**: `feat(sources): fetch_histogram for classic histograms; explain native histograms in query (4ok.3)`

---

### Task 3: Distribution storage

**Files:**
- Modify: `src/telemetry_nerd/datasets/db.py`, `src/telemetry_nerd/datasets/store.py`
- Test: `tests/unit/test_dataset_store.py` (append; reuse its `store` fixture)

**Interfaces:**
- `DatasetMeta` gains:
  - `scheme: dict | None = None`
  - `histogram: dict | None = None` (`{"selector", "by"}`)
  - `source_caveats: list[str] = field(default_factory=list)`

  Old stored metas still load with the defaults.
- `DatasetStore.meta(id) -> DatasetMeta` (raises `NotFound`)
- `DatasetStore.series_count(id) -> int`
- `DatasetStore.put_distribution(*, source, rng, step_ms, resolution_ms, dist, histogram, n_min) -> DatasetMeta`
- `DatasetStore.get_distribution(id) -> tuple[DatasetMeta, DistResult]`
- `DatasetStore.get(id)` raises `ValueError` for distribution datasets.

- [ ] **Step 1: Failing tests**

```python
import math

from telemetry_nerd.analysis.histogram import from_matrix


def _dist():
    return from_matrix(
        "s",
        [
            {"metric": {"le": "1"}, "values": [[60, "3"]]},
            {"metric": {"le": "+Inf"}, "values": [[60, "5"], [120, "0"]]},
            {"metric": {"le": "1"}, "values": [[120, "0"]]},
        ],
        expr="e",
    )


def test_distribution_round_trip_keeps_infinite_edges_and_zero_columns(store):
    meta = store.put_distribution(
        source="s", rng=TimeRange(60_000, 120_000), step_ms=60_000, resolution_ms=15_000,
        dist=_dist(), histogram={"selector": "x_bucket", "by": []}, n_min=20,
    )  # fmt: skip
    m2, d2 = store.get_distribution(meta.id)
    assert m2.representation == "distribution" and m2.n_min == 20
    assert m2.scheme["kind"] == "classic" and m2.histogram == {"selector": "x_bucket", "by": []}
    assert [(r["bucket_lo"], r["bucket_hi"], r["count"]) for r in d2.rows.to_pylist()] == [
        (-math.inf, 1.0, 3.0),
        (1.0, math.inf, 2.0),
    ]
    assert [(c["ts_ms"], c["n"]) for c in d2.columns.to_pylist()] == [(60_000, 5.0), (120_000, 0.0)]
    assert d2.scheme.edges == (1.0,)
    assert store.series_count(meta.id) == 1
    with pytest.raises(ValueError, match="distribution"):
        store.get(meta.id)
```

The first matrix item lists `le="1"` only at 60 s and a later item adds 120 s; `from_matrix` merges values per (series, ts), so this is fine.

- [ ] **Step 2: Run to verify failure**: `uv run pytest tests/unit/test_dataset_store.py -q` (`put_distribution` is missing).
- [ ] **Step 3: Implement**

`db.py`: append to `_SCHEMA`:

```python
    """CREATE TABLE IF NOT EXISTS dist_rows (
        dataset_id VARCHAR, ts_ms BIGINT, series_id VARCHAR,
        bucket_lo DOUBLE, bucket_hi DOUBLE, count DOUBLE)""",
    """CREATE TABLE IF NOT EXISTS dist_columns (
        dataset_id VARCHAR, ts_ms BIGINT, series_id VARCHAR, n DOUBLE)""",
```

`store.py`:
- Import `field` from dataclasses.
- Import `COLUMN_SCHEMA, DIST_SCHEMA, BucketScheme, DistResult` from `telemetry_nerd.model.distribution`.
- Add the three fields to `DatasetMeta`.
- Factor the `INSERT INTO datasets` and the `SELECT meta` code into `_insert_meta(con, meta)` and `meta(dataset_id)`.
- Then add:

```python
    def put_distribution(
        self,
        *,
        source: str,
        rng: TimeRange,
        step_ms: int,
        resolution_ms: int,
        dist: DistResult,
        histogram: dict,
        n_min: int,
    ) -> DatasetMeta:
        meta = DatasetMeta(
            id=self._new_id("d"),
            source=source,
            expr=dist.expr,
            start_ms=rng.start_ms,
            end_ms=rng.end_ms,
            step_ms=step_ms,
            resolution_ms=resolution_ms,
            representation="distribution",
            created_at_ms=self._clock(),
            n_min=n_min,
            scheme=dist.scheme.to_dict(),
            histogram=histogram,
            source_caveats=list(dist.caveats),
        )
        con = self._con
        con.begin()
        try:
            self._insert_meta(meta)
            for name, table, sql in (
                ("_tn_dr", dist.rows,
                 "INSERT INTO dist_rows SELECT $id, ts_ms, series_id, bucket_lo, bucket_hi, count FROM _tn_dr"),
                ("_tn_dc", dist.columns,
                 "INSERT INTO dist_columns SELECT $id, ts_ms, series_id, n FROM _tn_dc"),
            ):  # fmt: skip
                con.register(name, table)
                try:
                    con.execute(sql, {"id": meta.id})
                finally:
                    con.unregister(name)
            upsert_series(con, dist.series)
            con.commit()
        except Exception:
            con.rollback()
            raise
        return meta

    def get_distribution(self, dataset_id: str) -> tuple[DatasetMeta, DistResult]:
        meta = self.meta(dataset_id)
        if meta.representation != "distribution":
            raise ValueError(f"dataset {dataset_id} is {meta.representation}, not a distribution")
        params = {"id": dataset_id}
        rows = fetch_arrow(
            self._con,
            """SELECT ts_ms, series_id, bucket_lo, bucket_hi, count FROM dist_rows
               WHERE dataset_id = $id ORDER BY series_id, ts_ms, bucket_lo, bucket_hi""",
            params,
            DIST_SCHEMA,
        )
        columns = fetch_arrow(
            self._con,
            "SELECT ts_ms, series_id, n FROM dist_columns WHERE dataset_id = $id ORDER BY series_id, ts_ms",
            params,
            COLUMN_SCHEMA,
        )
        series = fetch_arrow(
            self._con,
            """SELECT series_id, labels FROM series WHERE series_id IN (
                 SELECT DISTINCT series_id FROM dist_columns WHERE dataset_id = $id)
               ORDER BY series_id""",
            params,
            SERIES_SCHEMA,
        )
        scheme = BucketScheme.from_dict(meta.scheme)
        return meta, DistResult(rows, columns, series, scheme, meta.expr, tuple(meta.source_caveats))

    def series_count(self, dataset_id: str) -> int:
        table = (
            "dist_columns"
            if self.meta(dataset_id).representation == "distribution"
            else "dataset_rows"
        )
        row = self._con.execute(
            f"SELECT COUNT(DISTINCT series_id) FROM {table} WHERE dataset_id = $id",  # noqa: S608 fixed names
            {"id": dataset_id},
        ).fetchone()
        return int(row[0]) if row else 0
```

In `get()`, after loading the meta, add: `if meta.representation == "distribution": raise ValueError(f"dataset {dataset_id} is a distribution; use get_distribution")`.

- [ ] **Step 4: Run** `just test && just lint`
- [ ] **Step 5: Commit**: `feat(datasets): store distribution datasets with empty vs zero columns (4ok.3)`

---

### Task 4: `query_distribution` service, summary, MCP tool, HTTP route

**Files:**
- Modify: `src/telemetry_nerd/core/summary.py`, `src/telemetry_nerd/core/service.py`, `src/telemetry_nerd/mcp/server.py`, `src/telemetry_nerd/api/app.py`
- Test: `tests/unit/test_service_distribution.py` (new), `tests/unit/test_summary.py` (append), `tests/unit/test_api.py` (append), `tests/unit/test_mcp.py` (if tools are enumerated there, add the new name)

**Interfaces:**
- `TelemetryService.query_distribution(selector, by=(), start="now-1h", end="now", step="auto", source="default", actor="claude") -> {dataset, summary}`
  - step floor = 2 × resolution (`SourceError` "two scrape intervals" if smaller)
  - auto step targets 300 columns
  - ≤ `MAX_BUCKETS_PER_QUERY` columns
  - emits event `dataset.created`
- `summarize_distribution(meta, dist, *, now_ms, settle_ms, top=5) -> dict`. Example (what Claude sees):

```json
{"dataset":"d7","expr":"sum by (le, vmrange, cloud_region) (increase(traces_spanmetrics_latency{...}[1m]))",
 "range":["2026-10-01T09:50:00+00:00","2026-10-01T10:20:00+00:00"],"step":"1m","representation":"distribution",
 "buckets":"native exponential, schema 3 (each bucket x1.09)","n_min":20,
 "counts":"increase() per step; additive over time and adjacent buckets",
 "series_count":3,"series":[{"labels":{"cloud_region":"ap-south-1"},"n_total":2304.0,"columns":24,
   "zero_columns":0,"missing_columns":7,"low_n_columns":3,"busiest":{"at":"2026-10-01T10:06:00+00:00","n":412.0},
   "quantile_buckets":{"p50":[0.297,0.324],"p90":[0.5,0.545],"p99":[14.67,16.0]}}, "…"],
 "more_series":0,"caveats":["gaps","low_count","estimated_counts"]}
```
- MCP tool `query_distribution(selector, by=None, start, end, step, source)`
- `POST /api/query-distribution {selector, by?, start?, end?, step?, source?}`

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_service_distribution.py
import json

import pytest

from telemetry_nerd.sources.base import SourceError
from tests.unit.fakes import FakeSource, make_service


async def test_query_distribution_builds_dataset_and_summary(tmp_path):
    src = FakeSource()
    svc = make_service(tmp_path, src)
    out = await svc.query_distribution(
        'lat_seconds_bucket{job="a"}', by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    meta, _ = svc.datasets.get_distribution(out["dataset"])
    assert meta.expr == 'sum by (le, vmrange, instance) (increase(lat_seconds_bucket{job="a"}[1m]))'
    assert meta.histogram == {"selector": 'lat_seconds_bucket{job="a"}', "by": ["instance"]}
    s = out["summary"]
    assert s["representation"] == "distribution" and s["n_min"] == 20
    assert s["buckets"] == "classic le buckets: 0.1, 1, 10"
    row = s["series"][0]  # sorted by n_total: instance i1 (x2)
    assert row["labels"] == {"instance": "i1"}
    assert (row["columns"], row["missing_columns"], row["low_n_columns"]) == (61, 0, 0)
    assert row["n_total"] == 200 * 61
    assert row["quantile_buckets"] == {"p50": ["-Inf", 0.1], "p90": ["-Inf", 0.1], "p99": [0.1, 1.0]}
    assert len(json.dumps(out)) < 2048
    assert src.hist_selectors == ['lat_seconds_bucket{job="a"}']


async def test_distribution_step_must_cover_two_scrapes(tmp_path):
    svc = make_service(tmp_path, FakeSource(resolution_ms=30_000))
    with pytest.raises(SourceError, match="two scrape"):
        await svc.query_distribution("x_bucket", start="now-2h", end="now-1h", step="30s")
    out = await svc.query_distribution("x_bucket", start="now-2h", end="now-1h")
    assert svc.datasets.meta(out["dataset"]).step_ms >= 60_000


async def test_unknown_source_is_explained(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError, match="unknown source"):
        await svc.query_distribution("x_bucket", source="nope")
```

Append to `tests/unit/test_summary.py`:

```python
def _dist(cum_by_ts, *, start=60_000, end=180_000, step=60_000):
    from telemetry_nerd.analysis.histogram import from_matrix

    by_le: dict[str, list] = {}
    for t, cum in cum_by_ts.items():
        for le, v in cum.items():
            by_le.setdefault(le, []).append([t / 1000, str(v)])
    dist = from_matrix("s", [{"metric": {"le": le}, "values": v} for le, v in by_le.items()], "e")
    m = DatasetMeta(
        id="d1", source="s", expr="e", start_ms=start, end_ms=end, step_ms=step,
        resolution_ms=15_000, representation="distribution", n_min=20,
        scheme=dist.scheme.to_dict(), source_caveats=list(dist.caveats),
    )  # fmt: skip
    return m, dist


def test_distribution_summary_counts_columns_and_bounds_quantiles():
    from telemetry_nerd.core.summary import summarize_distribution

    m, dist = _dist({60_000: {"1": 15, "+Inf": 30}, 120_000: {"1": 0, "+Inf": 0}})
    s = summarize_distribution(m, dist, now_ms=NOW, settle_ms=0)
    [row] = s["series"]
    assert s["buckets"] == "classic le buckets: 1"
    assert (row["n_total"], row["columns"], row["zero_columns"], row["missing_columns"]) == (30, 2, 1, 1)
    assert row["quantile_buckets"] == {"p50": ["-Inf", 1.0]}  # p90 needs n >= 100
    assert s["caveats"][0] == "gaps"
    assert "overflow" in s["caveats"]


def test_distribution_summary_flags_low_n_columns():
    from telemetry_nerd.core.summary import summarize_distribution

    m, dist = _dist({60_000: {"1": 3, "+Inf": 5}}, end=60_000)
    s = summarize_distribution(m, dist, now_ms=NOW, settle_ms=0)
    assert s["series"][0]["low_n_columns"] == 1
    assert s["series"][0]["quantile_buckets"] == {}
    assert "low_count" in s["caveats"]
```

Append to `tests/unit/test_api.py`:

```python
def test_query_distribution_route(client):
    r = client.post("/api/query-distribution", json={"selector": "x_bucket", "by": ["instance"], "start": "now-2h", "end": "now-1h"})
    assert r.status_code == 200 and r.json()["summary"]["representation"] == "distribution"
    bad = client.post("/api/query-distribution", json={"selector": "x_bucket", "by": "instance"})
    assert bad.status_code == 400
```

- [ ] **Step 2: Run to verify failure**: `uv run pytest tests/unit/test_service_distribution.py tests/unit/test_summary.py -q`
- [ ] **Step 3: Implement**

`summary.py`: extract the first three caveat checks of `summarize` into `_base_caveats(meta, now_ms, settle_ms) -> list[str]` and use it in `summarize`. Then add:

```python
import math

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.model.distribution import DistResult

QUANTILE_BOUNDS = (0.5, 0.9, 0.99)


def _edge(x: float) -> float | str:
    if math.isinf(x):
        return "+Inf" if x > 0 else "-Inf"
    return _round(x)


def _quantile_bucket(buckets: list[tuple[float, float, float]], q: float) -> list | None:
    """The source bucket that contains quantile q: honest bounds, never interpolated."""
    total = sum(c for _, _, c in buckets)
    if total <= 0:
        return None
    acc = 0.0
    for lo, hi, c in sorted(buckets, key=lambda b: (b[1], b[0])):
        acc += c
        if acc >= q * total * (1 - 1e-12):
            return [_edge(lo), _edge(hi)]
    return None


def summarize_distribution(
    meta: DatasetMeta, dist: DistResult, *, now_ms: int, settle_ms: int, top: int = 5
) -> dict:
    """Counts per bucket per step: n, coverage, low-n columns, and where the quantiles lie
    (bucket bounds, only where n is enough). Never a percentile value."""
    caveats = _base_caveats(meta, now_ms, settle_ms)
    caveats += [c for c in meta.source_caveats if c not in caveats]
    base = {
        "dataset": meta.id,
        "expr": meta.expr,
        "range": [iso(meta.start_ms), iso(meta.end_ms)],
        "step": format_duration(meta.step_ms),
        "representation": meta.representation,
        "buckets": (meta.scheme or {}).get("description", "no buckets"),
        "n_min": meta.n_min,
        "counts": "increase() per step; additive over time and adjacent buckets",
    }
    if dist.columns.num_rows == 0:
        return {**base, "series_count": 0, "series": [], "more_series": 0, "caveats": ["empty", *caveats]}
    expected = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    n_min = meta.n_min or 0
    labels = {r["series_id"]: json.loads(r["labels"]) for r in dist.series.to_pylist()}
    per = (
        pl.from_arrow(dist.columns)
        .group_by("series_id")
        .agg(
            pl.col("n").sum().alias("n_total"),
            pl.len().alias("columns"),
            (pl.col("n") == 0).sum().alias("zero_columns"),
            ((pl.col("n") > 0) & (pl.col("n") < n_min)).sum().alias("low_n_columns"),
            pl.col("ts_ms").sort_by("n").last().alias("busiest_ts"),
            pl.col("n").max().alias("busiest_n"),
        )
        .with_columns(
            pl.max_horizontal(pl.lit(expected) - pl.col("columns"), pl.lit(0)).alias("missing_columns")
        )
        .sort("n_total", descending=True)
    )
    merged: dict[str, list[tuple[float, float, float]]] = {}
    for r in (
        pl.from_arrow(dist.rows)
        .group_by("series_id", "bucket_lo", "bucket_hi")
        .agg(pl.col("count").sum())
        .iter_rows(named=True)
    ):
        merged.setdefault(r["series_id"], []).append((r["bucket_lo"], r["bucket_hi"], r["count"]))
    if per["missing_columns"].sum() > 0:
        caveats.insert(0, "gaps")
    if per["low_n_columns"].sum() > 0:
        caveats.append("low_count")
    if any(hi == math.inf for b in merged.values() for _, hi, _ in b):
        caveats.append("overflow")
    series = []
    for r in per.head(top).to_dicts():
        buckets = merged.get(r["series_id"], [])
        series.append(
            {
                "labels": labels.get(r["series_id"], {}),
                "n_total": _round(r["n_total"]),
                "columns": int(r["columns"]),
                "zero_columns": int(r["zero_columns"]),
                "missing_columns": int(r["missing_columns"]),
                "low_n_columns": int(r["low_n_columns"]),
                "busiest": {"at": iso(r["busiest_ts"]), "n": _round(r["busiest_n"])},
                "quantile_buckets": {
                    f"p{q * 100:g}": _quantile_bucket(buckets, q)
                    for q in QUANTILE_BOUNDS
                    if r["n_total"] >= min_samples(q)
                },
            }
        )
    return {
        **base,
        "series_count": per.height,
        "series": series,
        "more_series": max(0, per.height - top),
        "caveats": caveats,
    }
```

`service.py`:
- Factor the unknown-source block of `query` into `def _source(self, name) -> Source`.
- Import `summarize_distribution`, `DIST_N_MIN`, `format_duration`, `iso`.
- Add:

```python
DIST_TARGET_COLUMNS = 300

    async def query_distribution(
        self,
        selector: str,
        by: Sequence[str] = (),
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
        actor: Actor = "claude",
    ) -> dict:
        src = self._source(source)
        now = self.clock()
        rng = TimeRange(parse_time(start, now), parse_time(end, now))
        floor = 2 * src.resolution_ms  # increase() needs two samples per window
        step_ms = auto_step(rng, floor, DIST_TARGET_COLUMNS) if step == "auto" else parse_duration(step)
        if step_ms < floor:
            raise SourceError(
                f"step {format_duration(step_ms)} is shorter than two scrape intervals "
                f"({format_duration(floor)})",
                hint=f"counts come from increase() per step; use step >= {format_duration(floor)} or auto",
            )
        rng = rng.align(step_ms)
        columns = (rng.end_ms - rng.start_ms) // step_ms + 1
        if columns > MAX_BUCKETS_PER_QUERY:
            raise LimitExceeded(
                f"{columns} columns exceeds {MAX_BUCKETS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        dist = await src.fetch_histogram(selector, tuple(by), rng, step_ms)
        meta = self.datasets.put_distribution(
            source=src.name, rng=rng, step_ms=step_ms, resolution_ms=src.resolution_ms,
            dist=dist, histogram={"selector": selector.strip(), "by": list(by)}, n_min=DIST_N_MIN,
        )  # fmt: skip
        summary = summarize_distribution(meta, dist, now_ms=now, settle_ms=self.cache.settle_ms)
        self.log.append(actor, "dataset.created", meta.id, {"expr": meta.expr})
        return {"dataset": meta.id, "summary": summary}
```

`mcp/server.py`: add the tool after `query`:

```python
    @mcp.tool()
    async def query_distribution(
        selector: str,
        by: list[str] | None = None,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
    ) -> str:
        """Fetch a histogram as a distribution dataset: counts per value bucket per step.

        selector: the histogram metric with label filters, no functions:
          classic   http_server_request_duration_seconds_bucket{job="api"}   (le buckets)
          VictoriaMetrics  x_bucket{...}                                      (vmrange buckets)
          native    traces_spanmetrics_latency{service="checkout"}           (no _bucket suffix)
        by: labels kept as separate series (e.g. ["cloud_region"]); all others are summed.
        Counts are increase() over a window equal to the step (never from quantiles), so
        they add up over time and across adjacent buckets. step: auto (~300 columns), never
        shorter than two scrape intervals.
        Returns {dataset, summary}: n per series, columns with/without data, low-n columns,
        the bucket scheme (the resolution limit) and the BUCKET holding p50/p90/p99 where n
        is large enough. Draw it with `show` (heatmap), or `show(mark="histogram",
        windows=[...])` to compare windows.
        """
        try:
            return _dump(await service.query_distribution(selector, by or [], start, end, step, source))
        except SourceError as e:
            raise ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e)) from e
        except ValueError as e:
            raise ToolError(str(e)) from e
```

`api/app.py`: add a route next to `query`:

```python
    async def query_distribution(request: Request) -> JSONResponse:
        try:
            body = await _body(request, selector=str)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        by = body.get("by", [])
        if not isinstance(by, list) or not all(isinstance(x, str) for x in by):
            return _error(400, "by must be a list of label names", hint='e.g. ["cloud_region"]')
        args = {k: body[k] for k in ("start", "end", "step", "source") if k in body}
        try:
            out = await service.query_distribution(body["selector"], by, **args, actor="user")
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(out)
```

Register it with `Route("/api/query-distribution", query_distribution, methods=["POST"])`, and add the path to the parametrized origin/content-type tests if they enumerate POST routes.

- [ ] **Step 4: Run** `just test && just lint`
- [ ] **Step 5: Commit**: `feat: query_distribution with an honest distribution summary for Claude (4ok.3)`

---

### Task 5: Distribution LOD, heatmap panel data, spec marks, render budget

**Files:**
- Create: `src/telemetry_nerd/analysis/distlod.py`
- Modify: `src/telemetry_nerd/charts/spec.py`, `src/telemetry_nerd/core/service.py` (`show`, `panel_data`), `src/telemetry_nerd/api/app.py` (render report)
- Test: `tests/unit/test_distlod.py` (new), `tests/unit/test_chart_spec.py`, `tests/unit/test_service_distribution.py`, `tests/unit/test_api.py` (append)

**Interfaces:**
- `PX_PER_COLUMN = 2`, `PX_PER_ROW = 4`, `PX_PER_CELL = 8`, `FACET_HEIGHT_SINGLE = 260`, `FACET_HEIGHT_MULTI = 140`
- `rebucket_time(rows: pl.DataFrame, cols: pl.DataFrame, new_step_ms) -> (rows, cols)`. Counts, `n` and `cover` are summed; ts snaps to ceil(ts/k)·k.
- `merge_values(rows: pl.DataFrame, scheme: BucketScheme, max_rows: int) -> (rows, factor)`. Per series it merges whole source buckets. Classic/custom keep every m-th edge (Tasks 9/10 add native/vmrange). Open buckets are unchanged.
- `charts/spec.py`:
  - `Window(start_ms, end_ms, label="")`
  - `Layer.mark: Literal["line+envelope","heatmap","histogram","ecdf"]`
  - `Layer.windows: list[Window] = []`
  - `Layer.color: Literal["count","density"] = "count"`
  - `auto_spec(..., representation="bucket_agg")`
  - `validate(spec, series_counts, representations=None)`
  - constants `DISTRIBUTION_MARKS`, `FACET_BUDGET = 12`
- `TelemetryService.show(dataset_id, question, actor, unit=None, mark="auto", windows=None)`. The `mark`/`windows` arguments are wired in Task 13; for now `mark` defaults from the representation.
- `panel_data` returns `kind: "time"` for existing datasets. For heatmaps it returns `{kind:"heatmap", panel, dataset, effective_step_ms, value_merge, facet_height_px, series:[{id, labels, ts, n, cover, cells:{ts, lo, hi, c}}], caveats}`.
- Render report: an optional `height_px`. When present the budget is `points > width_px * height_px / PX_PER_CELL`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_distlod.py
import math

import polars as pl
from hypothesis import given
from hypothesis import strategies as st

from telemetry_nerd.analysis.distlod import merge_values, rebucket_time
from telemetry_nerd.model.distribution import BucketScheme

INF = math.inf


def _rows(buckets, counts, sid="a", ts=1):
    return pl.DataFrame(
        {
            "series_id": [sid] * len(buckets),
            "ts_ms": [ts] * len(buckets),
            "bucket_lo": [float(b[0]) for b in buckets],
            "bucket_hi": [float(b[1]) for b in buckets],
            "count": [float(c) for c in counts],
        }
    )


def test_time_rebucket_sums_counts_n_and_cover():
    rows = pl.DataFrame(
        {"series_id": ["a"] * 3, "ts_ms": [60_000, 120_000, 180_000],
         "bucket_lo": [0.0] * 3, "bucket_hi": [1.0] * 3, "count": [1.0, 2.0, 4.0]}
    )  # fmt: skip
    cols = pl.DataFrame(
        {"series_id": ["a"] * 3, "ts_ms": [60_000, 120_000, 180_000],
         "n": [1.0, 2.0, 4.0], "cover": [1, 1, 1]}
    )  # fmt: skip
    r, c = rebucket_time(rows, cols, 120_000)
    assert r.select("ts_ms", "count").rows() == [(120_000, 3.0), (240_000, 4.0)]
    assert c.select("ts_ms", "n", "cover").rows() == [(120_000, 3.0, 2), (240_000, 4.0, 1)]


def test_classic_merge_keeps_every_mth_edge_and_open_buckets():
    edges = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
    buckets = [(-INF, 0.1), (0.1, 0.2), (0.2, 0.5), (0.5, 1.0), (5.0, 10.0), (10.0, INF)]
    out, m = merge_values(_rows(buckets, [1, 2, 3, 4, 5, 6]), BucketScheme("classic", edges=edges), 3)
    assert m == 2
    assert out.select("bucket_lo", "bucket_hi", "count").rows() == [
        (-INF, 0.1, 1.0), (0.1, 0.5, 5.0), (0.5, 2.0, 4.0), (2.0, 10.0, 5.0), (10.0, INF, 6.0)
    ]  # fmt: skip


def test_no_merge_when_rows_fit():
    rows = _rows([(0.1, 0.2)], [1])
    out, m = merge_values(rows, BucketScheme("classic", edges=(0.1, 0.2)), 10)
    assert m == 1 and out.equals(rows)


@given(st.lists(st.integers(0, 50), min_size=2, max_size=12), st.integers(1, 6))
def test_merge_preserves_total_and_respects_the_row_budget(counts, max_rows):
    edges = tuple(float(2**i) for i in range(len(counts) + 1))
    rows = _rows(list(zip(edges[:-1], edges[1:], strict=True)), counts)
    out, _ = merge_values(rows, BucketScheme("classic", edges=edges), max_rows)
    assert out["count"].sum() == sum(counts)
    assert out.height <= max_rows
```

Append to `tests/unit/test_chart_spec.py`:

```python
def test_auto_spec_draws_distributions_as_heatmaps():
    assert auto_spec("d1", representation="distribution").layers[0].mark == "heatmap"


def test_marks_must_match_the_representation():
    lines = validate(auto_spec("d1"), {"d1": 1}, {"d1": "distribution"})
    assert "mark_representation" in [i.rule for i in lines if i.severity == "error"]
    heat = validate(auto_spec("d1", representation="distribution"), {"d1": 1}, {"d1": "bucket_agg"})
    assert "mark_representation" in [i.rule for i in heat if i.severity == "error"]


def test_heatmap_small_multiple_budget():
    spec = auto_spec("d1", representation="distribution")
    assert [i.rule for i in validate(spec, {"d1": 12}, {"d1": "distribution"}) if i.severity == "error"] == []
    assert "series_budget" in [i.rule for i in validate(spec, {"d1": 13}, {"d1": "distribution"})]


def test_histogram_needs_windows():
    spec = ChartSpec(layers=[Layer(mark="histogram", data="d1")])
    assert "windows" in [i.rule for i in validate(spec, {"d1": 1}, {"d1": "distribution"})]
```

Append to `tests/unit/test_service_distribution.py`:

```python
async def test_heatmap_panel_data_merges_time_and_keeps_every_count(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_bucket", by=["instance"], start="now-6h", end="now-1h", step="1m")
    res = svc.show(out["dataset"], "How is latency distributed over time?")
    assert res.panel.spec["layers"][0]["mark"] == "heatmap"
    data = svc.panel_data(res.panel.id, width_px=100)  # 301 columns -> <= 50
    assert data["kind"] == "heatmap"
    assert data["effective_step_ms"] == 7 * 60_000
    assert data["facet_height_px"] == 140
    s = {x["labels"]["instance"]: x for x in data["series"]}["i0"]
    assert sum(s["n"]) == 100 * 301 and sum(s["cells"]["c"]) == 100 * 301
    assert sum(s["cover"]) == 301
    assert len(s["ts"]) <= 50


async def test_time_panels_report_their_kind(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("up", "now-2h", "now-1h", step="1m")
    assert svc.panel_data(svc.show(out["dataset"], "Up?").panel.id, 800)["kind"] == "time"
```

Append to `tests/unit/test_api.py`:

```python
def test_render_report_heatmap_budget_uses_area(client):
    ok = {"panel_id": "p1", "render_ms": 20, "points": 2000, "width_px": 400, "height_px": 260}
    assert client.post("/api/render-report", json=ok).json()["budget_exceeded"] is False
    bad = ok | {"points": 400 * 260 // 8 + 1}
    assert client.post("/api/render-report", json=bad).json()["budget_exceeded"] is True
```

- [ ] **Step 2: Run to verify failure**: `uv run pytest tests/unit/test_distlod.py tests/unit/test_chart_spec.py tests/unit/test_service_distribution.py tests/unit/test_api.py -q`
- [ ] **Step 3: Implement**

```python
# src/telemetry_nerd/analysis/distlod.py
"""Level of detail for distributions (spec §6.5).

Counts are additive, so summing columns in time and merging adjacent value buckets is
exact. Merged buckets are unions of whole source buckets: never finer than the source
and never split.
"""

from __future__ import annotations

import bisect
import math

import polars as pl

from telemetry_nerd.model.distribution import BucketScheme

PX_PER_COLUMN = 2
PX_PER_ROW = 4
PX_PER_CELL = PX_PER_COLUMN * PX_PER_ROW
FACET_HEIGHT_SINGLE = 260
FACET_HEIGHT_MULTI = 140
_KEYS = ["series_id", "ts_ms", "bucket_lo", "bucket_hi"]

Mapping = list[tuple[float, float]] | None


def rebucket_time(
    rows: pl.DataFrame, cols: pl.DataFrame, new_step_ms: int
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Columns end at multiples of new_step_ms (as resample.rebucket); counts, n and cover
    (number of source columns with data) are summed."""
    k = new_step_ms
    end = ((pl.col("ts_ms") + k - 1) // k * k).alias("ts_ms")
    rows = rows.with_columns(end).group_by(_KEYS).agg(pl.col("count").sum()).sort(_KEYS)
    cols = (
        cols.with_columns(end)
        .group_by(["series_id", "ts_ms"])
        .agg(pl.col("n").sum(), pl.col("cover").sum())
        .sort(["series_id", "ts_ms"])
    )
    return rows, cols


def _edge_mapping(lo, hi, edges: tuple[float, ...], max_rows: int) -> tuple[Mapping, int]:
    n_finite = len(edges) - 1
    if n_finite < 1:
        return None, 1
    m = math.ceil(n_finite / max_rows)
    if m <= 1:
        return None, 1
    kept = list(edges[::m])
    if kept[-1] != edges[-1]:
        kept.append(edges[-1])
    out = []
    for low, high in zip(lo, hi, strict=True):
        if not (math.isfinite(low) and math.isfinite(high)):
            out.append((low, high))
            continue
        j = bisect.bisect_left(kept, high - 1e-12 * abs(high))  # first kept edge >= high
        out.append((kept[j - 1], kept[j]))
    return out, m


def _mapping(lo, hi, scheme: BucketScheme, max_rows: int) -> tuple[Mapping, int]:
    if scheme.kind in ("classic", "custom"):
        return _edge_mapping(lo, hi, scheme.edges, max_rows)
    return None, 1  # native: Task 9; vmrange: Task 10


def merge_values(
    rows: pl.DataFrame, scheme: BucketScheme, max_rows: int
) -> tuple[pl.DataFrame, int]:
    """Merge adjacent value buckets so each series has at most ~max_rows finite buckets."""
    if rows.height == 0 or max_rows < 1:
        return rows, 1
    parts, factor, changed = [], 1, False
    for part in rows.partition_by("series_id", maintain_order=True):
        mapped, m = _mapping(
            part["bucket_lo"].to_list(), part["bucket_hi"].to_list(), scheme, max_rows
        )
        factor = max(factor, m)
        if mapped is not None:
            changed = True
            part = part.with_columns(
                pl.Series("bucket_lo", [a for a, _ in mapped], dtype=pl.Float64),
                pl.Series("bucket_hi", [b for _, b in mapped], dtype=pl.Float64),
            )
        parts.append(part)
    if not changed:
        return rows, 1
    out = pl.concat(parts).group_by(_KEYS).agg(pl.col("count").sum()).sort(_KEYS)
    return out.select(rows.columns), factor
```

`charts/spec.py`:

```python
Mark = Literal["line+envelope", "heatmap", "histogram", "ecdf"]
DISTRIBUTION_MARKS = {"heatmap", "histogram", "ecdf"}
FACET_BUDGET = 12
MAX_WINDOWS = 4


class Window(BaseModel):
    start_ms: int
    end_ms: int
    label: str = ""


class Layer(BaseModel):
    mark: Mark
    data: str
    windows: list[Window] = Field(default_factory=list)  # histogram/ecdf: windows compared
    color: Literal["count", "density"] = "count"  # heatmap colour
```

`auto_spec` gains `representation: str = "bucket_agg"` and uses `mark = "heatmap" if representation == "distribution" else "line+envelope"`. `validate` gains `representations: dict[str, str] | None = None`; after the unknown-dataset check, loop over the layers:

```python
    reps = representations or {}
    for layer in spec.layers:
        rep = reps.get(layer.data)
        if rep is not None and (layer.mark in DISTRIBUTION_MARKS) != (rep == "distribution"):
            issues.append(ValidationIssue(
                rule="mark_representation", severity="error",
                message=(
                    f"{layer.mark} cannot draw {layer.data} ({rep}): distributions "
                    "(query_distribution) are drawn as heatmap, histogram or ecdf; "
                    "series datasets as lines"
                ),
            ))  # fmt: skip
        if layer.mark in ("histogram", "ecdf") and not 1 <= len(layer.windows) <= MAX_WINDOWS:
            issues.append(ValidationIssue(
                rule="windows", severity="error",
                message=f"{layer.mark} needs 1 to {MAX_WINDOWS} time windows to compare",
            ))  # fmt: skip
        if layer.mark in DISTRIBUTION_MARKS and series_counts.get(layer.data, 0) > FACET_BUDGET:
            issues.append(ValidationIssue(
                rule="series_budget", severity="error",
                message=(
                    f"{series_counts[layer.data]} series would be {series_counts[layer.data]} "
                    f"small multiples; at most {FACET_BUDGET}: group by fewer labels (by=[...])"
                ),
            ))  # fmt: skip
```

The existing line budget counts only `line+envelope` layers, as it does today.

`service.py`:
- `show`: get the meta with `self.datasets.meta(dataset_id)` and the series count with `self.datasets.series_count(dataset_id)`. Call `auto_spec(..., representation=meta.representation)` and `validate(spec, {dataset_id: n}, {dataset_id: meta.representation})`.
- `panel_data`: extract `_labels(series_table) -> dict` from the existing loop, then:

```python
    def panel_data(self, panel_id: str, width_px: int) -> dict:
        panel = self.workspace.get_panel(panel_id)
        meta = self.datasets.meta(panel.dataset_ids[0])
        if meta.representation == "distribution":
            return self._distribution_panel_data(panel, meta.id, width_px)
        ...  # existing body unchanged, plus "kind": "time" in the returned dict

    def _distribution_panel_data(self, panel: Panel, dataset_id: str, width_px: int) -> dict:
        meta, dist = self.datasets.get_distribution(dataset_id)
        caveats = summarize_distribution(meta, dist, now_ms=self.clock(), settle_ms=self.cache.settle_ms)["caveats"]
        rows = pl.from_arrow(dist.rows)
        cols = pl.from_arrow(dist.columns).with_columns(pl.lit(1, pl.Int64).alias("cover"))
        labels = _labels(dist.series)
        n_cols = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        factor = max(1, math.ceil(n_cols / max(1, width_px // PX_PER_COLUMN)))
        step = meta.step_ms * factor
        if factor > 1:
            rows, cols = rebucket_time(rows, cols, step)
        facet_h = FACET_HEIGHT_SINGLE if len(labels) <= 1 else FACET_HEIGHT_MULTI
        rows, value_merge = merge_values(rows, dist.scheme, max(4, facet_h // PX_PER_ROW))
        series = []
        for sid, lb in labels.items():
            c = cols.filter(pl.col("series_id") == sid)
            r = rows.filter(pl.col("series_id") == sid)
            series.append({
                "id": sid, "labels": lb,
                "ts": c["ts_ms"].to_list(), "n": c["n"].to_list(), "cover": c["cover"].to_list(),
                "cells": {"ts": r["ts_ms"].to_list(), "lo": r["bucket_lo"].to_list(),
                          "hi": r["bucket_hi"].to_list(), "c": r["count"].to_list()},
            })  # fmt: skip
        return {
            "kind": "heatmap", "panel": panel.to_dict(), "dataset": meta.to_dict(),
            "effective_step_ms": step, "value_merge": value_merge, "facet_height_px": facet_h,
            "series": series, "caveats": caveats,
        }  # fmt: skip
```

`JSONResponse` already turns ±Inf into `null`. Document this in `api.ts` in Task 6.

`api/app.py` `render_report`: import `PX_PER_CELL` from `analysis.distlod`, then:

```python
            height = body.get("height_px")
            limit = (
                body["width_px"] * height / PX_PER_CELL
                if isinstance(height, int | float) and height > 0
                else RENDER_POINTS_PER_PX * body["width_px"]
            )
            exceeded = body["render_ms"] > RENDER_BUDGET_MS or body["points"] > limit
```

- [ ] **Step 4: Run** `just test && just lint`
- [ ] **Step 5: Commit**: `feat: heatmap panel data with exact count LOD; distribution marks in the chart spec (4ok.5)`

---

### Task 6: UI pure modules: colormap, value axis, heatmap layout

**Files:**
- Create: `ui/src/chart/colormap.ts`, `ui/src/chart/heatmap.ts`, `ui/src/chart/axis.ts` and their `*.test.ts`
- Modify: `ui/src/lib/api.ts`

**Interfaces:**
- `colormap(name: "viridis" | "cividis"): (t: number) => string`, `relativeLuminance(color: string): number`
- `valueAxis(lo, hi, length, mode?) -> ValueAxis`, `layoutHeatmap(series, opts) -> HeatLayout`, `hitTest(layout, px, py) -> number | null`, `timeAt(layout, px) -> ms`, `STRIP_PX = 10`
- `valueTicks(axis) -> number[]`, `fmtValue(v, unit) -> string`
- `api.ts`:
  - `HeatCells`, `HeatSeries`, `WindowHist` (Task 13 fills it), `TimePanelData`, `HeatmapPanelData`, `HistogramPanelData`, and `PanelData` as a union on `kind`.
  - `DatasetMeta` gains `scheme?`, `histogram?`, `source_caveats?`.
  - `fetchPanelData` unchanged.
  - Document `lo: null` = −Inf and `hi: null` = +Inf.

- [ ] **Step 1: Generate colormap anchors** (17 samples each; no project dependency change):

```bash
uv run --with matplotlib python -c "import matplotlib as m; [print(n, [m.colors.to_hex(m.colormaps[n](i / 16)) for i in range(17)]) for n in ('viridis', 'cividis')]"
```

Paste the two lists into `ANCHORS` below. Viridis must start `#440154` and end `#fde725`; cividis must start `#00224e` and end `#fee838`.

- [ ] **Step 2: Failing tests**

```ts
// ui/src/chart/colormap.test.ts
import { describe, expect, it } from "vitest";
import { colormap, relativeLuminance } from "./colormap";

describe("colormap", () => {
  it("has the published endpoints", () => {
    expect(colormap("viridis")(0)).toBe("rgb(68,1,84)");
    expect(colormap("viridis")(1)).toBe("rgb(253,231,37)");
    expect(colormap("cividis")(0)).toBe("rgb(0,34,78)");
  });
  it.each(["viridis", "cividis"] as const)("%s is monotonic in luminance (perceptually ordered)", (name) => {
    const f = colormap(name);
    const l = Array.from({ length: 65 }, (_, i) => relativeLuminance(f(i / 64)));
    l.slice(1).forEach((v, i) => expect(v).toBeGreaterThan(l[i]));
  });
  it("clamps out-of-range and non-finite input", () => {
    const f = colormap("viridis");
    expect(f(-1)).toBe(f(0));
    expect(f(2)).toBe(f(1));
    expect(f(Number.NaN)).toBe(f(0));
  });
});
```

```ts
// ui/src/chart/heatmap.test.ts
import { describe, expect, it } from "vitest";
import { hitTest, layoutHeatmap, timeAt, valueAxis } from "./heatmap";
import type { HeatSeries } from "../lib/api";

const series = (cells: [number, number | null, number | null, number][], ts: number[], n: number[]): HeatSeries => ({
  id: "a", labels: {}, ts, n, cover: ts.map(() => 1),
  cells: { ts: cells.map((c) => c[0]), lo: cells.map((c) => c[1]), hi: cells.map((c) => c[2]), c: cells.map((c) => c[3]) },
});

describe("valueAxis", () => {
  it("is log when positive edges span two decades, with strips for open buckets", () => {
    const a = valueAxis([null, 0.005, 10], [0.005, 0.1, null], 200);
    expect(a.kind).toBe("log");
    expect([a.under, a.over]).toEqual([true, true]);
    expect(a.pos(0.005)).toBeCloseTo(a.body[0]);
    expect(a.pos(10)).toBeCloseTo(a.body[1]);
    expect(a.pos(0.1)).toBeGreaterThan(a.pos(0.005));
  });
  it("stays linear for a narrow range", () => {
    const a = valueAxis([1, 2], [2, 5], 100);
    expect(a.kind).toBe("linear");
    expect([a.under, a.over]).toEqual([false, false]);
    expect(a.pos(1)).toBe(0);
    expect(a.pos(5)).toBe(100);
  });
});

describe("layoutHeatmap", () => {
  const opts = { width: 300, height: 100, startMs: 60_000, endMs: 180_000, stepMs: 60_000, nMin: 20, color: "count" as const };
  it("draws columns as (ts - step, ts]; missing columns differ from zero columns", () => {
    // 60s: 30 obs; 120s: zero (n=0, no cells); 180s: no data
    const l = layoutHeatmap(series([[60_000, 1, 10, 20], [60_000, 10, 100, 10]], [60_000, 120_000], [30, 0]), opts);
    expect(l.rects.map((r) => [r.x, r.w])).toEqual([[0, 100], [0, 100]]);
    expect(l.rects[0]).toMatchObject({ y: 50, h: 50 }); // [1, 10] is the lower half of a log 1..100 axis
    expect(l.missing).toEqual([{ x: 200, w: 100 }]);
    expect(l.lowN).toEqual([]);
  });
  it("flags low-n columns", () => {
    const l = layoutHeatmap(series([[60_000, 1, 10, 5]], [60_000], [5]), opts);
    expect(l.lowN).toEqual([{ x: 0, w: 100 }]);
    expect(l.rects[0].lowN).toBe(true);
  });
  it("colours by count (monotonic) or by share of the column", () => {
    const s = series([[60_000, 1, 10, 20], [60_000, 10, 100, 10], [120_000, 1, 10, 10]], [60_000, 120_000], [30, 10]);
    const byCount = layoutHeatmap(s, opts).rects.map((r) => r.t);
    expect(byCount[0]).toBe(1);
    expect(byCount[1]).toBeLessThan(byCount[0]);
    expect(layoutHeatmap(s, { ...opts, color: "density" }).rects[2].t).toBe(1);
  });
  it("puts open buckets in strips", () => {
    const l = layoutHeatmap(series([[60_000, null, 0.01, 1], [60_000, 0.01, 10, 1], [60_000, 10, null, 1]], [60_000], [3]), opts);
    expect(l.rects[2]).toMatchObject({ y: 0, h: 10 }); // > 10: top strip
    expect(l.rects[0]).toMatchObject({ y: 90, h: 10 }); // <= 0.01: bottom strip
  });
  it("hit-tests cells and maps x back to time", () => {
    const l = layoutHeatmap(series([[60_000, 1, 10, 20]], [60_000], [20]), opts);
    const r = l.rects[0];
    expect(hitTest(l, r.x + 1, r.y + 1)).toBe(0);
    expect(hitTest(l, 250, 50)).toBeNull();
    expect(timeAt(l, 150)).toBe(90_000);
  });
});
```

```ts
// ui/src/chart/axis.test.ts
import { describe, expect, it } from "vitest";
import { fmtValue, valueTicks } from "./axis";
import { valueAxis } from "./heatmap";

describe("axis", () => {
  it("puts log ticks on decades inside the range", () => {
    expect(valueTicks(valueAxis([0.005], [10], 200))).toEqual([0.01, 0.1, 1, 10]);
  });
  it("adds 2 and 5 ticks when the range is under two decades", () => {
    expect(valueTicks(valueAxis([1], [100], 200, "log"))).toEqual([1, 2, 5, 10, 20, 50, 100]);
  });
  it("formats seconds below 1 as ms", () => {
    expect(fmtValue(0.25, "s")).toBe("250 ms");
    expect(fmtValue(2.5, "s")).toBe("2.5 s");
    expect(fmtValue(1500, null)).toBe("1500");
  });
});
```

- [ ] **Step 3: Run to verify failure**: `just ui-test`
- [ ] **Step 4: Implement**

```ts
// ui/src/chart/colormap.ts
// Perceptually uniform colormaps only (spec §6.3). 17 anchors sampled from matplotlib at
// t = 0, 1/16, …, 1 (generator in plan Task 6 step 1); linear interpolation between them.
export type ColormapName = "viridis" | "cividis";

const ANCHORS: Record<ColormapName, string[]> = {
  viridis: [/* 17 hex values from the generator */],
  cividis: [/* 17 hex values from the generator */],
};

const rgb = (hex: string): number[] => {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};

export function colormap(name: ColormapName): (t: number) => string {
  const pts = ANCHORS[name].map(rgb);
  return (t: number) => {
    const x = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0)) * (pts.length - 1);
    const i = Math.min(pts.length - 2, Math.floor(x));
    const f = x - i;
    const a = pts[i], b = pts[i + 1];
    return `rgb(${a.map((v, j) => Math.round(v + (b[j] - v) * f)).join(",")})`;
  };
}

/** WCAG relative luminance of "rgb(r,g,b)" or "#rrggbb". */
export function relativeLuminance(color: string): number {
  const m = color.match(/\d+/g);
  const [r, g, b] = color.startsWith("#") ? rgb(color) : (m ?? []).slice(0, 3).map(Number);
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}
```

```ts
// ui/src/chart/heatmap.ts
import type { HeatSeries } from "../lib/api";

export const STRIP_PX = 10; // open buckets: (-Inf, e] and (e, +Inf) get a strip, never a fake range
export type AxisMode = "auto" | "log" | "linear";

export interface ValueAxis {
  kind: "log" | "linear";
  min: number;
  max: number;
  under: boolean; // low strip: (-Inf, e], and on log any bucket reaching <= 0
  over: boolean; // high strip: (e, +Inf)
  length: number;
  body: [number, number];
  pos(v: number): number; // px from the low end
}

export function valueAxis(lo: (number | null)[], hi: (number | null)[], length: number, mode: AxisMode = "auto"): ValueAxis {
  let posMin = Infinity, posMax = -Infinity, linMin = Infinity, linMax = -Infinity;
  let openLow = false, openHigh = false, nonPositive = false;
  for (let i = 0; i < lo.length; i++) {
    const l = lo[i], h = hi[i];
    if (l === null) openLow = true; else { linMin = Math.min(linMin, l); linMax = Math.max(linMax, l); }
    if (h === null) openHigh = true; else { linMin = Math.min(linMin, h); linMax = Math.max(linMax, h); }
    if (l !== null && l > 0) { posMin = Math.min(posMin, l); posMax = Math.max(posMax, h ?? l); }
    else if (l !== null) nonPositive = true;
  }
  const canLog = posMin < Infinity && posMax > posMin;
  // spec §6.2: auto log scale when positive data spans two decades or more
  const log = mode === "log" ? canLog : mode === "auto" && canLog && posMax / posMin >= 100;
  let min = 0, max = 1;
  if (log) { min = posMin; max = posMax; }
  else if (linMin < Infinity) { min = linMin; max = linMax > linMin ? linMax : linMin + 1; }
  const under = openLow || (log && nonPositive);
  const body: [number, number] = [under ? STRIP_PX : 0, length - (openHigh ? STRIP_PX : 0)];
  const frac = log
    ? (v: number) => (Math.log10(v) - Math.log10(min)) / (Math.log10(max) - Math.log10(min))
    : (v: number) => (v - min) / (max - min);
  return {
    kind: log ? "log" : "linear", min, max, under, over: openHigh, length, body,
    pos: (v) => body[0] + frac(v) * (body[1] - body[0]),
  };
}

function cellSpan(a: ValueAxis, l: number | null, h: number | null): [number, number] {
  if (h === null) return [a.length - STRIP_PX, a.length];
  if (l === null || (a.kind === "log" && l <= 0)) return [0, STRIP_PX];
  return [a.pos(l), a.pos(h)];
}

export interface Span { x: number; w: number }
export interface HeatRect { x: number; y: number; w: number; h: number; t: number; cell: number; lowN: boolean }
export interface HeatLayoutOpts {
  width: number; height: number; startMs: number; endMs: number; stepMs: number;
  nMin: number; color: "count" | "density"; yMode?: AxisMode;
}
export interface HeatLayout {
  axis: ValueAxis; rects: HeatRect[]; missing: Span[]; lowN: Span[];
  colorMax: number; x0Ms: number; spanMs: number; width: number; nAt: Map<number, number>;
}

export function layoutHeatmap(s: HeatSeries, o: HeatLayoutOpts): HeatLayout {
  const axis = valueAxis(s.cells.lo, s.cells.hi, o.height, o.yMode);
  const k = o.stepMs;
  const x0Ms = Math.ceil(o.startMs / k) * k - k; // left edge of the first column
  const x1Ms = Math.ceil(o.endMs / k) * k;
  const spanMs = x1Ms - x0Ms;
  const toX = (ms: number) => ((ms - x0Ms) * o.width) / spanMs;
  const col = (ts: number): Span => ({ x: toX(ts - k), w: toX(ts) - toX(ts - k) });
  const nAt = new Map(s.ts.map((t, i) => [t, s.n[i]]));
  const missing: Span[] = [];
  for (let t = x0Ms + k; t <= x1Ms; t += k) if (!nAt.has(t)) missing.push(col(t));
  const lowN = s.ts.filter((_, i) => s.n[i] > 0 && s.n[i] < o.nMin).map(col);
  const value = (i: number) => {
    if (o.color === "count") return s.cells.c[i];
    const n = nAt.get(s.cells.ts[i]) ?? 0;
    return n > 0 ? s.cells.c[i] / n : 0;
  };
  let colorMax = 0;
  for (let i = 0; i < s.cells.c.length; i++) colorMax = Math.max(colorMax, value(i));
  const scale = (v: number) =>
    colorMax <= 0 ? 0 : o.color === "count" ? Math.log1p(v) / Math.log1p(colorMax) : v / colorMax;
  const rects: HeatRect[] = s.cells.c.map((_, i) => {
    const ts = s.cells.ts[i];
    const [p0, p1] = cellSpan(axis, s.cells.lo[i], s.cells.hi[i]);
    const n = nAt.get(ts) ?? 0;
    return { ...col(ts), y: o.height - p1, h: p1 - p0, t: scale(value(i)), cell: i, lowN: n > 0 && n < o.nMin };
  });
  return { axis, rects, missing, lowN, colorMax, x0Ms, spanMs, width: o.width, nAt };
}

export function hitTest(l: HeatLayout, px: number, py: number): number | null {
  for (let i = l.rects.length - 1; i >= 0; i--) {
    const r = l.rects[i];
    if (px >= r.x && px < r.x + r.w && py >= r.y && py < r.y + r.h) return i;
  }
  return null;
}

export const timeAt = (l: HeatLayout, px: number): number => l.x0Ms + (px / l.width) * l.spanMs;
```

```ts
// ui/src/chart/axis.ts
import type { ValueAxis } from "./heatmap";

export function valueTicks(a: ValueAxis): number[] {
  if (a.kind === "linear") {
    const raw = (a.max - a.min) / 5;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
    const out: number[] = [];
    for (let v = Math.ceil(a.min / step) * step; v <= a.max + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
    return out;
  }
  const lo = Math.ceil(Math.log10(a.min) - 1e-9), hi = Math.floor(Math.log10(a.max) + 1e-9);
  const mults = hi - lo < 2 ? [1, 2, 5] : [1];
  const out: number[] = [];
  for (let e = lo - 1; e <= hi; e++)
    for (const m of mults) {
      const v = Number((m * 10 ** e).toPrecision(12));
      if (v >= a.min * (1 - 1e-9) && v <= a.max * (1 + 1e-9)) out.push(v);
    }
  return out;
}

const sig = (v: number) => String(Number(v.toPrecision(3)));

export function fmtValue(v: number, unit: string | null): string {
  if (unit === "s" && Math.abs(v) < 1 && v !== 0) return `${sig(v * 1000)} ms`;
  return unit ? `${sig(v)} ${unit}` : sig(v);
}
```

`api.ts` additions:

```ts
/** Distribution cells. lo === null means -Inf, hi === null means +Inf (JSON has no Infinity). */
export interface HeatCells { ts: number[]; lo: (number | null)[]; hi: (number | null)[]; c: number[] }
export interface HeatSeries {
  id: string; labels: Record<string, string>;
  ts: number[]; n: number[]; cover: number[]; cells: HeatCells;
}
export interface WindowHist {
  label: string; start_ms: number; end_ms: number; n: number; columns: number;
  lo: (number | null)[]; hi: (number | null)[]; c: number[];
}
export interface BucketSchemeInfo {
  kind: string; edges: number[]; schema: number | null; per_decade: number | null; description: string;
}
interface PanelDataBase { panel: Panel; dataset: DatasetMeta; caveats: string[] }
export interface TimePanelData extends PanelDataBase { kind: "time"; effective_step_ms: number; series: SeriesData[] }
export interface HeatmapPanelData extends PanelDataBase {
  kind: "heatmap"; effective_step_ms: number; value_merge: number; facet_height_px: number; series: HeatSeries[];
}
export interface HistogramPanelData extends PanelDataBase {
  kind: "histogram"; mark: "histogram" | "ecdf"; effective_step_ms: number; value_merge: number;
  series: { id: string; labels: Record<string, string>; windows: WindowHist[] }[];
}
export type PanelData = TimePanelData | HeatmapPanelData | HistogramPanelData;
```

Extend `DatasetMeta` with `scheme?: BucketSchemeInfo | null; histogram?: { selector: string; by: string[] } | null; source_caveats?: string[]`. Extend `ChartSpec.layers[]` with `windows?: { start_ms: number; end_ms: number; label: string }[]; color?: "count" | "density"`.

- [ ] **Step 5: Run** `just ui-test && just ui-check && just ui-build`. `ui-check` will flag `Panel.svelte` using `data.series` without narrowing. Fix it minimally there: inside the uPlot effect, `const d = data; if (!d || d.kind !== "time" || !el) return;` and use `d`.
- [ ] **Step 6: Commit**: `feat(ui): viridis/cividis, log value axis and heatmap layout (4ok.5)`

---

### Task 7: `HeatmapPlot` canvas, Panel wiring, notes

**Files:**
- Create: `ui/src/components/HeatmapPlot.svelte`
- Modify: `ui/src/Panel.svelte`, `ui/src/lib/panelNotes.ts` (+ test)

**Interfaces:**
- `HeatmapPlot` props: `{ data: HeatmapPanelData; series: HeatSeries; width: number; height: number; unit: string | null; color?: "count" | "density"; onRendered(ms, cells); onBrush({x0, x1, left, width}) }`. `x0`/`x1` are in seconds, like the uPlot brush.
- `panelNotes(caveats, {yScaledToData, nMin, representation})`. Adds texts for `estimated_counts`, `non_monotonic`, `missing_inf`, `overflow`, and a distribution wording for `low_count` and `gaps`.
- `describeShown` handles `representation === "distribution"`: `"Counts per {step} column and value bucket (colour), from increase() of the histogram; bins are the source buckets ({scheme.description})."`

- [ ] **Step 1: Failing test** (append to `ui/src/lib/panelNotes.test.ts`)

```ts
it("explains distribution caveats in plain words", () => {
  const notes = panelNotes(["gaps", "low_count", "estimated_counts", "overflow"], { yScaledToData: false, nMin: 20, representation: "distribution" });
  expect(notes.map((n) => n.key)).toEqual(["gaps", "low_count", "estimated_counts", "overflow"]);
  expect(notes[0].text).toContain("hatched");
  expect(notes[1].text).toContain("fewer than 20 observations");
  expect(notes[2].text).toContain("extrapolat");
  expect(notes[3].text).toContain("largest bucket");
});
it("describes a distribution panel", () => {
  expect(describeShown({ representation: "distribution", quantile: null, scheme: { kind: "classic", edges: [0.1, 1], schema: null, per_decade: null, description: "classic le buckets: 0.1, 1" } }, "1m"))
    .toContain("classic le buckets: 0.1, 1");
});
```

- [ ] **Step 2: Implement `panelNotes.ts`**
- Change `CAVEATS` values to `(nMin, dist) => string`.
- `gaps` (dist): "Some steps have no data; those columns are hatched (no data is not zero)."
- `low_count` (dist): `` `Columns with fewer than ${n} observations are dimmed and ticked: their shape is noise.` ``
- `estimated_counts`: "Counts are increase() estimates: Prometheus extrapolates within each step, so they are not whole numbers."
- `non_monotonic`: "Some cumulative bucket counts decreased (independent extrapolation or a reset); the running maximum was used, as histogram_quantile does."
- `missing_inf`: "The histogram has no +Inf bucket: observations above the largest bucket are missing and n is a lower bound."
- `overflow`: "Some observations are above the largest bucket edge; their values are unknown (top strip)."
- Thread `representation` through `caveatText(key, nMin, representation)` and `panelNotes`.
- `describeShown` takes `Pick<DatasetMeta, "representation" | "quantile" | "scheme">`.

- [ ] **Step 3: Implement `HeatmapPlot.svelte`**

```svelte
<script lang="ts">
  import { colormap } from "../chart/colormap";
  import { hitTest, layoutHeatmap, STRIP_PX, timeAt, type HeatLayout } from "../chart/heatmap";
  import { fmtValue, valueTicks } from "../chart/axis";
  import { seriesName } from "../chart/toUplot";
  import { fmtRange } from "../lib/format";
  import type { HeatmapPanelData, HeatSeries } from "../lib/api";

  let { data, series, width, height, unit, color = "count", onRendered, onBrush }: {
    data: HeatmapPanelData; series: HeatSeries; width: number; height: number; unit: string | null;
    color?: "count" | "density";
    onRendered: (ms: number, cells: number) => void;
    onBrush: (b: { x0: number; x1: number; left: number; width: number }) => void;
  } = $props();

  const AXIS_LEFT = 64;
  const AXIS_BOTTOM = 16;
  let canvas = $state<HTMLCanvasElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let drag = $state<{ from: number; to: number } | null>(null);
  let layout: HeatLayout | null = null;
  const plotW = $derived(Math.max(10, width - AXIS_LEFT));
  const plotH = $derived(Math.max(10, height - AXIS_BOTTOM));
  const cmap = colormap("viridis");

  function hatch(ctx: CanvasRenderingContext2D, x: number, w: number, h: number) {
    ctx.save(); ctx.beginPath(); ctx.rect(x, 0, w, h); ctx.clip(); ctx.beginPath();
    for (let d = -h; d < w; d += 6) { ctx.moveTo(x + d, h); ctx.lineTo(x + d + h, 0); }
    ctx.stroke(); ctx.restore();
  }

  $effect(() => {
    const el = canvas;
    if (!el) return;
    const t0 = performance.now();
    const dpr = window.devicePixelRatio || 1;
    el.width = Math.round(width * dpr); el.height = Math.round(height * dpr);
    el.style.width = `${width}px`; el.style.height = `${height}px`;
    const ctx = el.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const css = getComputedStyle(el);
    const v = (name: string) => css.getPropertyValue(name).trim();
    const l = layoutHeatmap(series, {
      width: plotW, height: plotH, startMs: data.dataset.start_ms, endMs: data.dataset.end_ms,
      stepMs: data.effective_step_ms, nMin: data.dataset.n_min ?? 0, color,
    });
    layout = l;
    ctx.save(); ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--grid"); ctx.lineWidth = 1;
    for (const m of l.missing) hatch(ctx, m.x, m.w, plotH); // no data: hatched, never "zero"
    for (const r of l.rects) {
      ctx.globalAlpha = r.lowN ? 0.45 : 1;
      ctx.fillStyle = cmap(r.t);
      ctx.fillRect(r.x, r.y, Math.max(r.w, 1), Math.max(r.h, 1));
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = v("--warn");
    for (const m of l.lowN) ctx.fillRect(m.x, plotH - 3, m.w, 3);
    // value axis
    ctx.fillStyle = v("--muted"); ctx.font = "10px sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (const t of valueTicks(l.axis)) ctx.fillText(fmtValue(t, unit), -4, plotH - l.axis.pos(t));
    if (l.axis.over) ctx.fillText(`> ${fmtValue(l.axis.max, unit)}`, -4, STRIP_PX / 2);
    if (l.axis.under) ctx.fillText(`≤ ${fmtValue(l.axis.min, unit)}`, -4, plotH - STRIP_PX / 2);
    // time axis: 5 ticks
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let i = 0; i <= 4; i++) {
      const x = (plotW * i) / 4;
      ctx.fillText(new Date(timeAt(l, x)).toISOString().slice(11, 16), x, plotH + 2);
    }
    if (drag) { ctx.fillStyle = "rgba(127,127,127,0.25)"; ctx.fillRect(Math.min(drag.from, drag.to), 0, Math.abs(drag.to - drag.from), plotH); }
    ctx.restore();
    onRendered(performance.now() - t0, l.rects.length);
  });

  const local = (e: MouseEvent) => {
    const b = canvas!.getBoundingClientRect();
    return { x: e.clientX - b.left - AXIS_LEFT, y: e.clientY - b.top };
  };
  function move(e: MouseEvent) {
    const p = local(e);
    if (drag) { drag = { ...drag, to: Math.max(0, Math.min(plotW, p.x)) }; return; }
    const i = layout ? hitTest(layout, p.x, p.y) : null;
    if (i === null || !layout) { tip = null; return; }
    const cell = layout.rects[i].cell;
    const ts = series.cells.ts[cell], lo = series.cells.lo[cell], hi = series.cells.hi[cell], c = series.cells.c[cell];
    const n = layout.nAt.get(ts) ?? 0;
    const edge = (x: number | null, inf: string) => (x === null ? inf : fmtValue(x, unit));
    tip = {
      x: p.x + AXIS_LEFT + 8, y: p.y + 8,
      text: `${fmtRange(ts - data.effective_step_ms, ts)} · (${edge(lo, "−∞")}, ${edge(hi, "+∞")}] · count ${Number(c.toPrecision(4))} · n ${Number(n.toPrecision(4))}${n > 0 ? ` (${((100 * c) / n).toPrecision(3)}%)` : ""}${n > 0 && n < (data.dataset.n_min ?? 0) ? " · low n" : ""}`,
    };
  }
  function up() {
    if (drag && layout && Math.abs(drag.to - drag.from) >= 3) {
      const a = Math.min(drag.from, drag.to), b = Math.max(drag.from, drag.to);
      onBrush({ x0: timeAt(layout, a) / 1000, x1: timeAt(layout, b) / 1000, left: a + AXIS_LEFT, width: b - a });
    }
    drag = null;
  }
</script>

<div class="facet">
  <div class="facet-label">{seriesName(series.labels)}</div>
  <canvas
    bind:this={canvas}
    class="heatmap"
    onmousedown={(e) => { const p = local(e); drag = { from: p.x, to: p.x }; }}
    onmousemove={move}
    onmouseup={up}
    onmouseleave={() => { tip = null; }}
  ></canvas>
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px">{tip.text}</div>{/if}
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; white-space: nowrap; z-index: 5; }
</style>
```

Panel.svelte (minimal, additive):
- Import `HeatmapPlot`.
- `const kind = $derived(data?.kind ?? "time")`.
- The uPlot effect returns early unless `kind === "time"` (already narrowed in Task 6).
- Inside the `.plot` div, before the `SelectionMenu` block:

```svelte
    {#if data && data.kind === "heatmap"}
      {@const hm = data}
      {#each hm.series as s, i (s.id)}
        <HeatmapPlot
          data={hm} series={s} width={fetchWidth} height={hm.facet_height_px}
          unit={hm.panel.spec.y.unit}
          onRendered={(ms, cells) => onFacetRendered(i, hm.series.length, ms, cells, hm.facet_height_px)}
          onBrush={(b) => (selection = { ...b, top: i * (hm.facet_height_px + 14) + 4 })}
        />
      {/each}
      <div class="legend">colour: count per bucket per {fmtStep(hm.effective_step_ms)} (log scale) · hatched: no data · dimmed: n &lt; {hm.dataset.n_min}{#if hm.value_merge > 1} · {hm.value_merge} source buckets per row{/if}</div>
    {/if}
```

`onFacetRendered` sums cells and the maximum ms over facets. When the last facet reports, call `reportRender({panel_id, render_ms, points: cells, width_px, height_px: facetH * n})` and set `render`. Pass `distribution={kind === "heatmap" || !!data?.dataset.histogram}` to `SelectionMenu`; the prop is consumed in Task 15, so add it to SelectionMenu's props as an optional, unused boolean now. Add `data-heatmap-cells` to the section for e2e. Pass `representation: data.dataset.representation` into `panelNotes`.

- [ ] **Step 4: Run** `just ui-test && just ui-check && just ui-build`
- [ ] **Step 5: Commit**: `feat(ui): canvas heatmap with no-data hatching, low-n columns and tooltip (4ok.5)`

---

### Task 8: Synthetic demo histogram, VictoriaMetrics integration test, live dev check (closes the slice)

**Files:**
- Modify: `src/telemetry_nerd/devtools/synthetic.py`, `tests/unit/test_synthetic.py`
- Create: `tests/integration/test_distribution_vm.py`

- [ ] **Step 1: Failing unit test** (append to `tests/unit/test_synthetic.py`)

```python
def test_demo_histogram_is_cumulative_and_counts_every_request():
    import re

    text = demo_text(0, 3_600_000)
    by_ts: dict[tuple[str, str], dict[str, float]] = {}
    pat = re.compile(r'^tn_demo_request_duration_seconds_bucket\{instance="(\w)",le="([^"]+)"\} (\S+) (\d+)$')
    for line in text.splitlines():
        if m := pat.match(line):
            by_ts.setdefault((m[1], m[4]), {})[m[2]] = float(m[3])
    assert by_ts
    for cum in by_ts.values():
        ordered = [cum[k] for k in sorted(cum, key=lambda k: float(k))]  # "+Inf" sorts last
        assert ordered == sorted(ordered)
```

- [ ] **Step 2: Implement** (in `synthetic.py`; keep `demo_text`'s existing series and seed order):

```python
LATENCY_LE = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _lognormal_cdf(x: float, median: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf(math.log(x / median) / (sigma * math.sqrt(2))))


def histogram_text(
    metric: str, labels: dict[str, str], scrapes: list[tuple[int, int, float]], sigma: float = 0.6
) -> str:
    """Classic cumulative _bucket counters. scrapes: (ts, requests since last scrape, median
    latency). Deterministic: per-bucket counts are rounded expectations, so cumulative
    counts never decrease across le."""
    edges = (*LATENCY_LE, math.inf)
    cum = dict.fromkeys(edges, 0)
    samples: dict[float, list[tuple[int, float]]] = {e: [] for e in edges}
    for ts, k, median in scrapes:
        for e in edges:
            cum[e] += k if math.isinf(e) else round(k * _lognormal_cdf(e, median, sigma))
            samples[e].append((ts, float(cum[e])))
    return "".join(
        exposition(f"{metric}_bucket", {**labels, "le": "+Inf" if math.isinf(e) else f"{e:g}"}, samples[e])
        for e in edges
    )
```

Note that `round(k*F)` accumulated per scrape is monotone in `le` per scrape, so it is monotone cumulatively too. In `demo_text`, collect `(ts, increment, value)` per instance while building the existing series (the increment is the existing `rng.randint(600, 900)`; the median is the latency value). Then append `histogram_text("tn_demo_request_duration_seconds", {"instance": instance}, scrapes)`. Do not add RNG calls, so the existing series stay identical.

- [ ] **Step 3: Integration test**

```python
# tests/integration/test_distribution_vm.py
import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import TimeRange, now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration

# observations per second, cumulative by le: 90% <= 0.1s, 9% in (0.1, 1], 1% in (1, 10]
CUMULATIVE = {"0.1": 9.0, "1": 9.9, "10": 10.0, "+Inf": 10.0}


async def _service(vm_url, tmp_path):
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(SourceSpec(name="vm", url=vm_url, flavor="victoriametrics"))
    return svc


async def test_classic_histogram_distribution_end_to_end(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    push(vm_url, "".join(
        exposition("tn_it_dist_seconds_bucket", {"le": le, "job": "it"},
                   [(t0 + i * 15_000, rate * 15 * i) for i in range(480)])
        for le, rate in CUMULATIVE.items()
    ))  # fmt: skip
    svc = await _service(vm_url, tmp_path)
    out = await svc.query_distribution(
        'tn_it_dist_seconds_bucket{job="it"}', start="now-100m", end="now-40m", step="1m", source="vm"
    )
    s = out["summary"]
    assert s["buckets"] == "classic le buckets: 0.1, 1, 10"
    [row] = s["series"]
    assert row["missing_columns"] == 0 and row["low_n_columns"] == 0
    assert row["quantile_buckets"] == {"p50": ["-Inf", 0.1], "p90": ["-Inf", 0.1], "p99": [0.1, 1.0]}
    meta, dist = svc.datasets.get_distribution(out["dataset"])
    # VictoriaMetrics increase() uses the sample before each window: columns tile exactly
    assert {c["n"] for c in dist.columns.to_pylist()} == {600.0}
    counts = await svc.sources.get("vm").fetch_values(
        'sum(increase(tn_it_dist_seconds_bucket{job="it",le="+Inf"}[1m]))',
        TimeRange(meta.start_ms, meta.end_ms), 60_000,
    )  # fmt: skip
    assert {round(r["avg"], 6) for r in counts.buckets.to_pylist()} == {600.0}
```

If VictoriaMetrics returns 599.x at the first column because of a sample boundary, assert the interior columns only, with a comment. Do not loosen the bucket assertions.

- [ ] **Step 4: Run** `just test && just lint && uv run pytest -m integration tests/integration/test_distribution_vm.py -q`
- [ ] **Step 5: Live dev check** (slice done):
  1. `just dev-up && just seed`, then restart the dev daemon once.
  2. Via `uv run python scripts/mcp_call.py --url http://127.0.0.1:7070/mcp`:
     - `query_distribution '{"selector":"tn_demo_request_duration_seconds_bucket","by":["instance"],"start":"now-3h","end":"now-10m"}'`
     - `show '{"dataset":"<d>","question":"How is demo latency distributed over time?","unit":"s"}'`
  3. Expected: three facets; instance c has a dense band near 1–2.5 s for 5 minutes; hatched columns where there is no data; panel attribute `data-budget-exceeded="false"`.
- [ ] **Step 6: Commit**: `feat: demo latency histogram and classic distribution end to end through VictoriaMetrics (4ok.3, 4ok.5)`

---

## Phase B: native histograms and vmrange

### Task 9: Native histograms (parse, scheme, value LOD)

**Files:** Modify `analysis/histogram.py`, `analysis/distlod.py`. Tests: `tests/unit/test_histogram.py`, `tests/unit/test_distlod.py` (append).

- [ ] **Step 1: Failing tests**

```python
# test_histogram.py (append)
from telemetry_nerd.analysis.histogram import native_schema


def b(i, schema=3):
    return repr(2 ** (i / 2**schema))


# Grafana Play, sum by (le, vmrange, cloud_region) (increase(traces_spanmetrics_latency{...}[1m])),
# 2026-10-01T10:02Z (bounds are 2^(i/8): schema 3)
NATIVE = [
    {"metric": {"cloud_region": "ap-south-1"}, "histograms": [[1790848920, {
        "count": "8.000800080008", "sum": "212.3",
        "buckets": [[0, b(31), b(32), "2.0"], [0, b(39), b(40), "2.0"], [0, b(40), b(41), "4.0"]]}]]},
    {"metric": {"cloud_region": "eu-west-1"}, "histograms": [[1790848920, {
        "count": "5", "sum": "1.86",
        "buckets": [[0, b(-14), b(-13), "1.25"], [0, b(-12), b(-11), "2.5"], [0, b(-10), b(-9), "1.25"]]}]]},
]  # fmt: skip


def test_native_matrix_uses_histogram_count_and_detects_schema():
    d = from_matrix("play", NATIVE)
    assert d.scheme == BucketScheme("native", schema=3)
    ap = series_id("play", {"cloud_region": "ap-south-1"})
    assert {c["series_id"]: c["n"] for c in d.columns.to_pylist()}[ap] == 8.000800080008
    rows = [(r["bucket_lo"], r["bucket_hi"], r["count"]) for r in d.rows.to_pylist() if r["series_id"] == ap]
    assert rows == [(2 ** (31 / 8), 16.0, 2.0), (2 ** (39 / 8), 32.0, 2.0), (32.0, 2 ** (41 / 8), 4.0)]
    assert "estimated_counts" in d.caveats


def test_native_schema_detection():
    assert native_schema([(2 ** (3 / 8), 2 ** (4 / 8)), (2 ** (1 / 4), 2 ** (2 / 4))]) == 2
    assert native_schema([(-1e-128, 1e-128)]) is None  # zero bucket only
    assert native_schema([(0.005, 0.01), (0.01, 0.025)]) is None  # custom (NHCB)
    assert native_schema([(1.0, math.inf)]) is None


def test_mixed_float_and_histogram_samples_are_refused():
    with pytest.raises(ValueError, match="mixes"):
        from_matrix("s", [{"metric": {}, "values": [[1, "1"]], "histograms": NATIVE[0]["histograms"]}])
```

```python
# test_distlod.py (append)
def test_native_merge_lowers_the_schema_exactly():
    buckets = [(2 ** ((i - 1) / 8), 2 ** (i / 8)) for i in range(1, 9)]
    out, m = merge_values(_rows(buckets, [1] * 8), BucketScheme("native", schema=3), 2)
    assert m == 4
    assert out.select("bucket_lo", "bucket_hi", "count").rows() == [
        (1.0, 2**0.5, 4.0), (2**0.5, 2.0, 4.0)
    ]  # fmt: skip


def test_native_merge_nests_mixed_schemas_on_the_coarse_grid():
    # one schema-2 bucket (2^(1/4), 2^(2/4)] and four schema-3 buckets, max 1 row
    buckets = [(2 ** (2 / 8), 2 ** (4 / 8))] + [(2 ** ((i - 1) / 8), 2 ** (i / 8)) for i in range(5, 9)]
    out, _ = merge_values(_rows(buckets, [1] * 5), BucketScheme("native", schema=2), 1)
    assert out.select("bucket_lo", "bucket_hi", "count").rows() == [(2 ** (0 / 8), 2.0, 5.0)]
```

The coarse grid at schema 2 is 2^(i/4). The span covers indices 1..4 (coarse), so with `max_rows=1`, m = 4 coarse buckets = 8 fine units. The fine index of 2^(8/8) is 8 → u = 8 → (2^0, 2^1] = (1, 2]. Every bucket falls in (1, 2], so the result is one row of 5.

- [ ] **Step 2: Implement** in `analysis/histogram.py`:

```python
def native_buckets(h: dict) -> tuple[list[Bucket], float]:
    """One native histogram sample {count, sum, buckets: [[rule, lo, hi, count], ...]}.
    n is the histogram's own count (histogram_count): it can differ from the bucket sum
    (Play: 8.0008 vs 8.0). Boundary rule 0 = (lo, hi]; the zero bucket is [-z, z]."""
    n = float(h["count"])
    out: list[Bucket] = []
    for _rule, lo, hi, c in h.get("buckets") or []:
        c = float(c)
        if math.isfinite(c) and c > 0:
            out.append((float(lo), float(hi), c))
    return out, n


def native_schema(pairs) -> int | None:
    """Coarsest exponential schema s (bucket growth 2^(2^-s)) with every bucket on the
    2^(i * 2^-s) grid; None for custom bounds (NHCB). The zero bucket is ignored."""
    schemas: set[int] = set()
    for lo, hi in pairs:
        if not (math.isfinite(lo) and math.isfinite(hi)):
            return None
        a, b = sorted((abs(lo), abs(hi)))
        if a == 0 or a == b:
            continue
        s = -math.log2(math.log2(b / a))
        if abs(s - round(s)) > 1e-6:
            return None
        idx = math.log2(b) * 2 ** round(s)
        if abs(idx - round(idx)) > 1e-6:
            return None
        schemas.add(round(s))
    return min(schemas) if schemas else None
```

In `from_matrix`, replace the `"histograms" in item` raise with:

```python
        if "histograms" in item and item.get("values"):
            raise ValueError("a series mixes float and native histogram samples (migration?); narrow the selector or time range")
        for sample in item.get("histograms") or []:
            forms.add("native")
            t = _ts(sample[0])
            buckets, n = native_buckets(sample[1])
            if not math.isfinite(n):
                continue
            cols[(sid, t)] = n
            for lo, hi, c in buckets:
                rows.append((t, sid, lo, hi, c))
                pairs.add((lo, hi))
```

In `_scheme`, replace the final raise with:

```python
    if form == "native":
        schema = native_schema(pairs)
        if schema is not None:
            return BucketScheme("native", schema=schema)
        return BucketScheme("custom", edges=tuple(sorted({e for p in pairs for e in p if math.isfinite(e)})))
    raise ValueError(f"unsupported histogram form {form}")
```

In `distlod.py`, add:

```python
def _native_mapping(lo, hi, max_rows: int) -> tuple[Mapping, int]:
    """Coarsen to a lower schema: merge 2^k adjacent buckets on the coarsest schema's grid,
    so buckets of different schemas in one series nest. Zero, negative and open buckets stay."""
    spec: list[int | None] = []
    for low, high in zip(lo, hi, strict=True):
        ok = low > 0 and math.isfinite(high) and high > low
        spec.append(round(-math.log2(math.log2(high / low))) if ok else None)
    known = [s for s in spec if s is not None]
    if not known:
        return None, 1
    fine, coarse = max(known), min(known)
    idx = [
        round(math.log2(high) * 2**fine) if s is not None else None
        for high, s in zip(hi, spec, strict=True)
    ]
    finite = [i for i in idx if i is not None]
    unit = 2 ** (fine - coarse)  # fine indices per coarsest bucket
    span = (max(finite) - min(finite)) // unit + 1
    m = 1 << max(0, math.ceil(math.log2(max(1.0, span / max_rows))))
    if m == 1:
        return None, 1
    width = m * unit
    out = []
    for low, high, i in zip(lo, hi, idx, strict=True):
        if i is None:
            out.append((low, high))
        else:
            u = -(-i // width) * width
            out.append((2.0 ** ((u - width) / 2**fine), 2.0 ** (u / 2**fine)))
    return out, m
```

and route `scheme.kind == "native"` to it in `_mapping`.

- [ ] **Step 3: Run** `just test && just lint`
- [ ] **Step 4: Commit**: `feat(analysis): native histograms: histogram_count n, schema detection, exact schema-lowering LOD (4ok.3)`

---

### Task 10: VictoriaMetrics `vmrange`

**Files:** Modify `analysis/histogram.py`, `analysis/distlod.py`. Tests: `test_histogram.py`, `test_distlod.py`, `tests/integration/test_distribution_vm.py` (append).

- [ ] **Step 1: Failing tests**

```python
# test_histogram.py (append)
from telemetry_nerd.analysis.histogram import parse_vmrange


def test_parse_vmrange():
    assert parse_vmrange("5.275e-02...5.995e-02") == (0.05275, 0.05995)
    assert parse_vmrange("1.000e+18...+Inf") == (1e18, math.inf)
    assert parse_vmrange("0...0") == (0.0, 0.0)
    with pytest.raises(ValueError):
        parse_vmrange("1..2")


# dev VictoriaMetrics: sum by (vmrange) (histogram_over_time(tn_demo_latency_seconds[5m])), step 5m
VMRANGE = [
    {"metric": {"vmrange": "5.275e-02...5.995e-02"}, "values": [[1790865000, "19"]]},
    {"metric": {"vmrange": "5.995e-02...6.813e-02"}, "values": [[1790864700, "10"], [1790865000, "38"]]},
    {"metric": {"vmrange": "6.813e-02...7.743e-02"}, "values": [[1790864400, "46"], [1790864700, "50"], [1790865000, "3"]]},
    {"metric": {"vmrange": "7.743e-02...8.799e-02"}, "values": [[1790864400, "14"]]},
]  # fmt: skip


def test_vmrange_columns_sum_the_present_ranges():
    d = from_matrix("vm", VMRANGE)
    assert d.scheme == BucketScheme("vmrange", per_decade=18)
    assert [(c["ts_ms"], c["n"]) for c in d.columns.to_pylist()] == [
        (1790864400000, 60.0), (1790864700000, 60.0), (1790865000000, 60.0)
    ]  # fmt: skip
    assert d.caveats == ()
```

```python
# test_distlod.py (append)
def test_vmrange_merge_groups_m_ranges():
    g = 10 ** (1 / 18)
    buckets = [(g ** (i - 1), g**i) for i in range(1, 7)] + [(0.0, 1e-9), (1e18, INF)]
    out, m = merge_values(_rows(buckets, [1] * 8), BucketScheme("vmrange", per_decade=18), 3)
    assert m == 2
    assert out["count"].sum() == 8.0
    finite = [r for r in out.select("bucket_lo", "bucket_hi").rows() if r[0] > 0 and r[1] < INF]
    assert len(finite) == 3
```

Integration test (append to `tests/integration/test_distribution_vm.py`):

```python
VMRANGES = {"1.000e-01...1.136e-01": 5, "1.000e+00...1.136e+00": 1}  # increments per 15s scrape


async def test_vmrange_histogram_distribution(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    push(vm_url, "".join(
        exposition("tn_it_vm_seconds_bucket", {"vmrange": r, "job": "it"},
                   [(t0 + i * 15_000, float(inc * i)) for i in range(480)])
        for r, inc in VMRANGES.items()
    ))  # fmt: skip
    svc = await _service(vm_url, tmp_path)
    out = await svc.query_distribution(
        'tn_it_vm_seconds_bucket{job="it"}', start="now-100m", end="now-40m", step="1m", source="vm"
    )
    assert out["summary"]["buckets"].startswith("VictoriaMetrics vmrange, 18 per decade")
    _, dist = svc.datasets.get_distribution(out["dataset"])
    assert {c["n"] for c in dist.columns.to_pylist()} == {24.0}
    assert sorted({(r["bucket_lo"], r["bucket_hi"]) for r in dist.rows.to_pylist()}) == [
        (0.1, 0.1136), (1.0, 1.136)
    ]  # fmt: skip
```

- [ ] **Step 2: Implement**

```python
def parse_vmrange(text: str) -> tuple[float, float]:
    """VictoriaMetrics bucket label "lo...hi" ("%.3e", "+Inf" allowed)."""
    lo, sep, hi = text.partition("...")
    if not sep:
        raise ValueError(f"malformed vmrange {text!r}")
    a, b = float(lo), float(hi)
    if not a <= b:
        raise ValueError(f"malformed vmrange {text!r}")
    return a, b
```

In `from_matrix`, replace the vmrange raise with:

```python
                forms.add("vmrange")
                lo, hi = parse_vmrange(vmrange)
                if not math.isfinite(x):
                    continue
                cols[(sid, t)] = cols.get((sid, t), 0.0) + x
                if x > 0:
                    rows.append((t, sid, lo, hi, x))
```

In `_scheme`, add `if form == "vmrange": return BucketScheme("vmrange", per_decade=VM_PER_DECADE)`.

`distlod.py`:

```python
def _vm_mapping(lo, hi, per_decade: int, max_rows: int) -> tuple[Mapping, int]:
    idx = [
        round(math.log10(high) * per_decade) if low > 0 and math.isfinite(high) else None
        for low, high in zip(lo, hi, strict=True)
    ]
    finite = [i for i in idx if i is not None]
    if not finite:
        return None, 1
    m = math.ceil((max(finite) - min(finite) + 1) / max_rows)
    if m <= 1:
        return None, 1
    out = []
    for low, high, i in zip(lo, hi, idx, strict=True):
        if i is None:
            out.append((low, high))
        else:
            u = -(-i // m) * m
            out.append((10 ** ((u - m) / per_decade), 10 ** (u / per_decade)))
    return out, m
```

Route `vmrange` to it in `_mapping`. Printed edges are rounded to 4 significant digits; `round()` on the log index absorbs that.

- [ ] **Step 3: Run** `just test && just lint && uv run pytest -m integration tests/integration/test_distribution_vm.py -q`
- [ ] **Step 4: Commit**: `feat(analysis): VictoriaMetrics vmrange distributions (4ok.3)`

---

### Task 11: Grafana Play preset, recorded fixtures, replay tests

**Files:**
- Modify: `src/telemetry_nerd/sources/presets.py`
- Create: `scripts/record_play_histograms.py`, `tests/fixtures/play/*`, `tests/unit/test_play_fixtures.py`

- [ ] **Step 1: Preset** (`presets.py`):

```python
PLAY = SourceSpec(
    name="play",
    url="https://play.grafana.org/api/datasources/proxy/uid/grafanacloud-prom",
    flavor="prometheus",
    resolution_ms=20_000,
    politeness=Politeness(max_concurrency=1, min_interval_ms=1000, timeout_s=60),
)
PRESETS = {"wikimedia": WIKIMEDIA, "play": PLAY}
```

- [ ] **Step 2: Record script.** Mirror `scripts/record_wikimedia.py`, with `OUT = tests/fixtures/play`. Use `NATIVE_RANGE = TimeRange(1_790_848_200_000, 1_790_850_000_000)` (09:50–10:20Z) at 1m, and `CLASSIC_RANGE = TimeRange(1_790_856_000_000, 1_790_859_600_000)` (12:00–13:00Z) at 5m. Run the four calls listed in "Fixtures to record" through `PromQLSource.from_spec(PLAY, client=…RecordingTransport…)`, using `fetch_histogram` / `fetch_values`, and print series and row counts. Run it once: `uv run python scripts/record_play_histograms.py`. That is about 4 requests, spaced by the gate. Commit the fixtures.
- [ ] **Step 3: Replay tests**

```python
# tests/unit/test_play_fixtures.py
"""Distribution datasets from recorded Grafana Play responses (native + classic): no network."""

from pathlib import Path

import httpx
import polars as pl
import pytest

from telemetry_nerd.analysis.distlod import rebucket_time
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.presets import PLAY
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import ReplayTransport

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "play"
NATIVE_SEL = 'traces_spanmetrics_latency{service="checkoutservice",span_kind="SPAN_KIND_SERVER"}'
NATIVE_RANGE = TimeRange(1_790_848_200_000, 1_790_850_000_000)  # as in scripts/record_play_histograms.py
CLASSIC_SEL = 'http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice"}'
CLASSIC_RANGE = TimeRange(1_790_856_000_000, 1_790_859_600_000)


@pytest.fixture
def play():
    return PromQLSource.from_spec(PLAY, client=httpx.AsyncClient(transport=ReplayTransport(FIXTURES)))


async def test_native_columns_equal_histogram_count(play):
    dist = await play.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
    assert dist.scheme.kind == "native" and dist.scheme.schema is not None
    assert dist.series.num_rows == 3
    counts = await play.fetch_values(
        f"sum by (cloud_region) (histogram_count(increase({NATIVE_SEL}[1m])))", NATIVE_RANGE, 60_000
    )
    want = {(r["series_id"], r["ts_ms"]): r["avg"] for r in counts.buckets.to_pylist() if r["avg"] is not None}
    got = {(c["series_id"], c["ts_ms"]): c["n"] for c in dist.columns.to_pylist()}
    assert got.keys() == want.keys()
    assert all(got[k] == pytest.approx(want[k]) for k in got)


async def test_spike_column_is_the_slow_cluster(play):
    from telemetry_nerd.model.series import series_id

    dist = await play.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
    ap = series_id("play", {"cloud_region": "ap-south-1"})
    rows = [r for r in dist.rows.to_pylist() if r["series_id"] == ap and r["ts_ms"] == 1_790_848_920_000]
    assert rows and min(r["bucket_lo"] for r in rows) > 10  # 10:02Z: all >= ~14.7 s
    n = next(c["n"] for c in dist.columns.to_pylist() if c["series_id"] == ap and c["ts_ms"] == 1_790_848_920_000)
    assert n == pytest.approx(8, abs=0.01)


async def test_time_rebucket_sums_native_counts(play):
    dist = await play.fetch_histogram(NATIVE_SEL, ["cloud_region"], NATIVE_RANGE, 60_000)
    rows = pl.from_arrow(dist.rows)
    cols = pl.from_arrow(dist.columns).with_columns(pl.lit(1, pl.Int64).alias("cover"))
    r5, c5 = rebucket_time(rows, cols, 300_000)
    assert r5["count"].sum() == pytest.approx(rows["count"].sum())
    assert c5["n"].sum() == pytest.approx(cols["n"].sum())


async def test_classic_play_n_equals_inf_bucket_increase(play):
    dist = await play.fetch_histogram(CLASSIC_SEL, [], CLASSIC_RANGE, 300_000)
    assert dist.scheme.kind == "classic"
    inf = await play.fetch_values(
        'sum(increase(http_server_request_duration_seconds_bucket{job="ecommerce-prod/cartservice",le="+Inf"}[5m]))',
        CLASSIC_RANGE, 300_000,
    )  # fmt: skip
    want = sorted(r["avg"] for r in inf.buckets.to_pylist())
    assert sorted(c["n"] for c in dist.columns.to_pylist()) == pytest.approx(want)
```

The 10:02Z assertions encode what was observed on 2026-10-01. If the recording differs (for example, data rolled off before recording), adjust the window to what was recorded and note it in the commit message.

- [ ] **Step 4: Run** `just test && just lint`
- [ ] **Step 5: Commit**: `test(sources): Grafana Play native and classic histogram fixtures; n equals histogram_count (4ok.3)`. Close 4ok.3 if its acceptance criteria are met: `bd close telemetry-nerd-4ok.3 --reason "classic/vmrange/native distribution datasets; Play + VM fixtures; additive LOD"`.

---

## Phase C: histogram chart (4ok.4)

### Task 12: Quantile datasets remember their histogram

**Files:** Modify `analysis/exprkind.py`, `core/service.py` (`query`). Test: `tests/unit/test_exprkind.py`, `tests/unit/test_service_distribution.py`.

**Interfaces:**
- `HistogramSource(selector: str, by: tuple[str, ...])`
- `histogram_source(expr) -> HistogramSource | None` for `histogram_quantile(q, sum by (L) (rate|increase(SEL[w])))`. `le` is dropped from L. Returns `None` for other shapes.
- `query` stores `histogram={"selector", "by"}` on quantile datasets via `DatasetStore.put(..., histogram=...)` (new kwarg).

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_exprkind.py (append)
from telemetry_nerd.analysis.exprkind import HistogramSource, histogram_source


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ('histogram_quantile(0.95, sum by (cloud_region) (rate(lat{svc="c"}[5m])))',
         HistogramSource('lat{svc="c"}', ("cloud_region",))),
        ("histogram_quantile(0.99, sum by (le, region) (increase(x_bucket[2m])))",
         HistogramSource("x_bucket", ("region",))),
        ("histogram_quantile(0.5, sum(rate(x_bucket[1m])))", HistogramSource("x_bucket", ())),
        ("histogram_quantile(0.5, rate(x_bucket[1m]))", None),  # no sum: per-series, cannot express as by
        ("quantile_over_time(0.9, q[5m])", None),
        ("sum(rate(x[5m]))", None),
        ('histogram_quantile(0.9, sum by (le) (rate(x_bucket{p="a[1m]"}[1m])))',
         HistogramSource('x_bucket{p="a[1m]"}', ())),
    ],
)  # fmt: skip
def test_histogram_source(expr, want):
    assert histogram_source(expr) == want
```

```python
# test_service_distribution.py (append)
async def test_quantile_datasets_remember_their_histogram(tmp_path):
    svc = make_service(tmp_path)
    q = await svc.query(
        'histogram_quantile(0.95, sum by (le, instance) (rate(lat_seconds_bucket{job="a"}[5m])))',
        "now-2h", "now-1h", step="1m",
    )  # fmt: skip
    assert svc.datasets.meta(q["dataset"]).histogram == {
        "selector": 'lat_seconds_bucket{job="a"}', "by": ["instance"]
    }  # fmt: skip
```

- [ ] **Step 2: Implement** (in `exprkind.py`, reusing `_mask_strings`, `_close`, `_split_args`, `_QCALL`):

```python
_SUM = re.compile(r"^\s*sum\s*(?:by\s*\(([^()]*)\)\s*)?\(")
_WINDOWED_CALL = re.compile(r"^\s*(?:rate|increase)\s*\(")
_ANY_SELECTOR = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*(\{[^{}]*\})?\s*$")


@dataclass(frozen=True)
class HistogramSource:
    selector: str
    by: tuple[str, ...]


def _unwrap(text: str, masked: str, pattern: re.Pattern) -> tuple[re.Match, str, str] | None:
    m = pattern.match(masked)
    if not m:
        return None
    open_idx = m.end() - 1
    close_idx = _close(masked, open_idx)
    if masked[close_idx + 1 :].strip():
        return None
    return m, text[open_idx + 1 : close_idx], masked[open_idx + 1 : close_idx]


def histogram_source(expr: str) -> HistogramSource | None:
    """The histogram behind histogram_quantile(q, sum by (L) (rate|increase(SEL[w]))), so a
    percentile panel can open the distribution it was computed from."""
    info = analyze(expr)
    if info.quantile is None or info.quantile.func != "histogram_quantile" or info.problem:
        return None
    masked = _mask_strings(expr)
    call = _QCALL.search(masked)
    open_idx = call.end() - 1
    args = _split_args(expr, masked, open_idx + 1, _close(masked, open_idx))
    inner = args[1]
    summed = _unwrap(inner, _mask_strings(inner), _SUM)
    if summed is None:
        return None
    m, body, mbody = summed
    by = tuple(x.strip() for x in (m.group(1) or "").split(",") if x.strip() and x.strip() != "le")
    windowed = _unwrap(body.strip(), mbody.strip(), _WINDOWED_CALL)
    if windowed is None:
        return None
    _, arg, marg = windowed
    bracket = marg.rfind("[")
    if bracket < 0 or not _ANY_SELECTOR.match(marg[:bracket]):
        return None
    return HistogramSource(arg[:bracket].strip(), by)
```

`marg` keeps positions with quoted text blanked out, so `rfind("[")` skips brackets inside label values. `body.strip()` and `mbody.strip()` keep their alignment because both strip the same whitespace.

`store.py` `put()`: add `histogram: dict | None = None` and pass it into `DatasetMeta`. `service.query`: on the quantile path, compute `hs = histogram_source(expr)` and pass `histogram={"selector": hs.selector, "by": list(hs.by)} if hs else None`.

- [ ] **Step 3: Run** `just test && just lint`
- [ ] **Step 4: Commit**: `feat(analysis): quantile datasets carry the histogram they come from (4ok.4)`

---

### Task 13: Window histograms, histogram/ECDF panels, `show(mark, windows)`, distribution follow-up endpoint

**Files:**
- Modify: `analysis/distlod.py` (`window_histogram`), `core/service.py` (`show`, `_distribution_panel_data`, `distribution_panel`), `mcp/server.py` (`show`), `api/app.py` (route)
- Test: `tests/unit/test_distlod.py`, `tests/unit/test_service_distribution.py`, `tests/unit/test_api.py`

**Interfaces:**
- `window_histogram(rows, cols, step_ms, start_ms, end_ms) -> dict[sid, {start_ms, end_ms, n, columns, lo, hi, c}]`. It takes the whole columns `(ts-step, ts]` that overlap the window; the window snaps outward.
- `show(dataset_id, question, actor="claude", unit=None, mark="auto", windows: list[Window] | None = None)`. Windows must lie within `[start_ms - step_ms, end_ms]`, otherwise `ValueError`.
- `panel_data` for `histogram`/`ecdf` marks returns `{kind:"histogram", mark, panel, dataset, effective_step_ms, value_merge, series:[{id, labels, windows:[WindowHist]}], caveats}`. Bars are value-merged to ≥ 3 px each (`HIST_PX_PER_BAR = 3`). `low_count` is added when any window has 0 < n < n_min.
- `async distribution_panel(panel_id, start_ms, end_ms, baseline="previous", actor="user") -> Panel`:
  - A distribution dataset is used as is.
  - A quantile dataset with `histogram` is fetched with `query_distribution` over the panel's range at `max(step, 2×res)`.
  - Otherwise it raises `SourceError` with a hint.
  - Windows: `selection` plus `previous` (same length, immediately before; dropped if outside the data).
  - Question: `"How are values distributed between HH:MMZ and HH:MMZ, compared with the preceding window?"`
- MCP `show(dataset, question, unit=None, mark="auto", windows=None)`. `windows` is a list of `{start, end, label}` in time syntax.
- `POST /api/panels/{id}/distribution {start_ms, end_ms, baseline?}` → `{panel}`.

- [ ] **Step 1: Failing tests**

```python
# test_distlod.py (append)
from telemetry_nerd.analysis.distlod import window_histogram


def test_window_takes_whole_overlapping_columns():
    rows = pl.DataFrame({"series_id": ["a"] * 4, "ts_ms": [60_000, 120_000, 180_000, 240_000],
                         "bucket_lo": [0.0] * 4, "bucket_hi": [1.0] * 4, "count": [1.0, 2.0, 4.0, 8.0]})
    cols = pl.DataFrame({"series_id": ["a"] * 4, "ts_ms": [60_000, 120_000, 180_000, 240_000],
                         "n": [1.0, 2.0, 4.0, 8.0]})  # fmt: skip
    w = window_histogram(rows, cols, 60_000, 70_000, 150_000)["a"]
    assert (w["start_ms"], w["end_ms"], w["columns"], w["n"]) == (60_000, 180_000, 2, 6.0)
    assert (w["lo"], w["hi"], w["c"]) == ([0.0], [1.0], [6.0])
```

Columns 120 k covers (60 k, 120 k] and 180 k covers (120 k, 180 k]; both overlap [70 k, 150 k]. Column 60 k covers (0, 60 k] and does not.

```python
# test_service_distribution.py (append)
from telemetry_nerd.charts.spec import Window
from telemetry_nerd.core.service import ChartRejected


async def test_histogram_panel_sums_whole_columns_in_window(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_bucket", by=["instance"], start="now-2h", end="now-1h", step="1m")
    meta = svc.datasets.meta(out["dataset"])
    a = meta.start_ms + 10 * 60_000 + 5_000
    res = svc.show(out["dataset"], "How is latency distributed at 10 past?", mark="histogram",
                   windows=[Window(start_ms=a, end_ms=a + 120_000, label="sel")])  # fmt: skip
    data = svc.panel_data(res.panel.id, 600)
    assert data["kind"] == "histogram" and data["mark"] == "histogram"
    w = {s["labels"]["instance"]: s["windows"][0] for s in data["series"]}["i1"]
    assert w["columns"] == 3 and w["n"] == 3 * 200
    assert (w["start_ms"], w["end_ms"]) == (meta.start_ms + 10 * 60_000, meta.start_ms + 13 * 60_000)
    assert w["c"] == [3 * 180.0, 3 * 18.0, 3 * 2.0] and w["hi"][0] == 0.1


async def test_histogram_marks_are_validated(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_bucket", start="now-2h", end="now-1h", step="1m")
    with pytest.raises(ChartRejected, match="windows"):
        svc.show(out["dataset"], "q?", mark="ecdf", windows=[])
    plain = await svc.query("up", "now-2h", "now-1h", step="1m")
    with pytest.raises(ChartRejected, match="distribution"):
        svc.show(plain["dataset"], "q?", mark="heatmap")
    meta = svc.datasets.meta(out["dataset"])
    with pytest.raises(ValueError, match="outside"):
        svc.show(out["dataset"], "q?", mark="histogram",
                 windows=[Window(start_ms=meta.end_ms + 1, end_ms=meta.end_ms + 60_000)])  # fmt: skip


async def test_distribution_from_a_quantile_panel_fetches_its_histogram(tmp_path):
    src = FakeSource()
    svc = make_service(tmp_path, src)
    q = await svc.query(
        'histogram_quantile(0.95, sum by (le, instance) (rate(lat_seconds_bucket{job="a"}[5m])))',
        "now-2h", "now-1h", step="1m",
    )  # fmt: skip
    panel = svc.show(q["dataset"], "p95 by instance?").panel
    m = svc.datasets.meta(q["dataset"])
    new = await svc.distribution_panel(panel.id, m.start_ms + 30 * 60_000, m.start_ms + 35 * 60_000)
    layer = new.spec["layers"][0]
    assert layer["mark"] == "histogram"
    assert [w["label"] for w in layer["windows"]] == ["selection", "previous"]
    assert src.hist_selectors == ['lat_seconds_bucket{job="a"}']
    assert "distributed between" in new.question


async def test_distribution_from_a_plain_panel_is_refused(tmp_path):
    svc = make_service(tmp_path)
    q = await svc.query("up", "now-2h", "now-1h", step="1m")
    panel = svc.show(q["dataset"], "Up?").panel
    with pytest.raises(SourceError, match="histogram"):
        await svc.distribution_panel(panel.id, 0, 60_000)
```

```python
# test_api.py (append)
def test_distribution_followup_route(client):
    ds = client.post("/api/query-distribution", json={"selector": "x_bucket", "start": "now-2h", "end": "now-1h"}).json()
    panel = client.post("/api/show", json={"dataset": ds["dataset"], "question": "Distribution?"}).json()["panel"]
    data = client.get(f"/api/panels/{panel['id']}/data?width=600").json()
    t = data["series"][0]["ts"][10]
    r = client.post(f"/api/panels/{panel['id']}/distribution", json={"start_ms": t - 120_000, "end_ms": t})
    assert r.status_code == 200 and r.json()["panel"]["spec"]["layers"][0]["mark"] == "histogram"
    assert client.post(f"/api/panels/{panel['id']}/distribution", json={"start_ms": "x"}).status_code == 400
```

- [ ] **Step 2: Implement**

```python
# distlod.py
HIST_PX_PER_BAR = 3


def window_histogram(
    rows: pl.DataFrame, cols: pl.DataFrame, step_ms: int, start_ms: int, end_ms: int
) -> dict[str, dict]:
    """Sum whole columns (ts - step, ts] that overlap [start, end], per series. The window
    snaps outward to those columns; n and counts are sums (additive)."""
    inside = (pl.col("ts_ms") > start_ms) & (pl.col("ts_ms") - step_ms < end_ms)
    c = (
        cols.filter(inside)
        .group_by("series_id")
        .agg(pl.col("n").sum(), pl.len().alias("columns"),
             pl.col("ts_ms").min().alias("first"), pl.col("ts_ms").max().alias("last"))
    )  # fmt: skip
    r = (
        rows.filter(inside)
        .group_by("series_id", "bucket_lo", "bucket_hi")
        .agg(pl.col("count").sum())
        .sort("series_id", "bucket_hi", "bucket_lo")
    )
    out: dict[str, dict] = {}
    for row in c.iter_rows(named=True):
        b = r.filter(pl.col("series_id") == row["series_id"])
        out[row["series_id"]] = {
            "start_ms": row["first"] - step_ms,
            "end_ms": row["last"],
            "n": row["n"],
            "columns": row["columns"],
            "lo": b["bucket_lo"].to_list(),
            "hi": b["bucket_hi"].to_list(),
            "c": b["count"].to_list(),
        }
    return out
```

`service.py`:
- `show`: after `auto_spec`, if `mark != "auto"`, set `spec.layers = [Layer(mark=mark, data=dataset_id, windows=windows or [])]`.
- For each window, check `meta.start_ms - meta.step_ms <= w.start_ms < w.end_ms <= meta.end_ms`; otherwise raise `ValueError(f"window {iso(w.start_ms)}..{iso(w.end_ms)} is outside the dataset range")`.
- `_distribution_panel_data`: when `panel.spec["layers"][0]["mark"] in ("histogram", "ecdf")`:

```python
            rows, value_merge = merge_values(pl.from_arrow(dist.rows), dist.scheme, max(4, width_px // HIST_PX_PER_BAR))
            cols = pl.from_arrow(dist.columns)
            layer = panel.spec["layers"][0]
            per_window = [
                (w, window_histogram(rows, cols, meta.step_ms, w["start_ms"], w["end_ms"]))
                for w in layer["windows"]
            ]
            series = []
            for sid, lb in labels.items():
                wins = []
                for w, hist in per_window:
                    h = hist.get(sid) or {"start_ms": w["start_ms"], "end_ms": w["end_ms"], "n": 0.0,
                                          "columns": 0, "lo": [], "hi": [], "c": []}  # fmt: skip
                    wins.append({"label": w["label"], **h})
                series.append({"id": sid, "labels": lb, "windows": wins})
            n_min = meta.n_min or 0
            if any(0 < w["n"] < n_min for s in series for w in s["windows"]) and "low_count" not in caveats:
                caveats.append("low_count")
            return {"kind": "histogram", "mark": layer["mark"], "panel": panel.to_dict(),
                    "dataset": meta.to_dict(), "effective_step_ms": meta.step_ms,
                    "value_merge": value_merge, "series": series, "caveats": caveats}  # fmt: skip
```

- `distribution_panel`:

```python
    async def distribution_panel(
        self, panel_id: str, start_ms: int, end_ms: int, baseline: str = "previous", actor: Actor = "user"
    ) -> Panel:
        if end_ms <= start_ms:
            raise ValueError("selection end must be after its start")
        panel = self.workspace.get_panel(panel_id)
        meta = self.datasets.meta(panel.dataset_ids[0])
        if meta.representation != "distribution":
            if not meta.histogram:
                raise SourceError(
                    "no histogram behind this panel",
                    hint="distributions come from histograms: use query_distribution on the _bucket or native histogram metric",
                )
            src = self._source(meta.source)
            step = max(meta.step_ms, 2 * src.resolution_ms)
            out = await self.query_distribution(
                meta.histogram["selector"], meta.histogram["by"], start=str(meta.start_ms),
                end=str(meta.end_ms), step=format_duration(step), source=meta.source, actor=actor,
            )  # fmt: skip
            meta = self.datasets.meta(out["dataset"])
        windows = [Window(start_ms=start_ms, end_ms=end_ms, label="selection")]
        span = end_ms - start_ms
        if baseline == "previous" and start_ms - span >= meta.start_ms - meta.step_ms:
            windows.append(Window(start_ms=start_ms - span, end_ms=start_ms, label="previous"))
        a, b = iso(start_ms)[11:16], iso(end_ms)[11:16]
        question = f"How are values distributed between {a}Z and {b}Z" + (
            ", compared with the preceding window?" if len(windows) > 1 else "?"
        )
        unit = (panel.spec.get("y") or {}).get("unit")
        return self.show(meta.id, question, actor=actor, unit=unit, mark="histogram", windows=windows).panel
```

`api/app.py`:

```python
    async def panel_distribution(request: Request) -> JSONResponse:
        try:
            body = await _body(request, start_ms=int, end_ms=int)
        except _BadRequest as e:
            return _error(e.status, str(e), hint=e.hint)
        baseline = body.get("baseline", "previous")
        try:
            panel = await service.distribution_panel(
                request.path_params["id"], body["start_ms"], body["end_ms"],
                baseline if baseline in ("previous", "none") else "previous",
            )  # fmt: skip
        except NotFound as e:
            return _error(404, str(e))
        except ChartRejected as e:
            return _error(422, "chart rejected", issues=[i.model_dump() for i in e.issues])
        except SourceError as e:
            return _error(400, str(e), hint=e.hint)
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse({"panel": panel.to_dict()})
```

Register it with `Route("/api/panels/{id}/distribution", panel_distribution, methods=["POST"])`. Check how `_body` validates `int`; if it accepts only the listed types via `isinstance`, note that `bool` is an `int` and reject it explicitly.

MCP `show`: add `mark: str = "auto"` and `windows: list[dict] | None = None`. Parse each `{start, end, label}` with `parse_time(str(...), service.clock())` into `Window`. Update the docstring:

```
        mark: auto (heatmap for distributions, lines otherwise), heatmap, histogram, ecdf.
        windows (histogram/ecdf): 1-4 [{start, end, label}] compared on one chart, e.g. the
        spike vs the preceding baseline; each window sums whole steps, n is shown per window.
```

- [ ] **Step 3: Run** `just test && just lint`
- [ ] **Step 4: Commit**: `feat: histogram/ECDF panels over time windows; distribution follow-up from heatmap or percentile panels (4ok.4)`

---

### Task 14: UI pure modules: bars, ECDF, edge gap

**Files:** Create `ui/src/chart/distribution.ts` and `ui/src/chart/distribution.test.ts`.

- [ ] **Step 1: Failing tests**

```ts
import { describe, expect, it } from "vitest";
import { bars, ecdf, maxEcdfGapAtEdges } from "./distribution";
import type { WindowHist } from "../lib/api";

const w = (lo: (number | null)[], hi: (number | null)[], c: number[], label = "w"): WindowHist => ({
  label, start_ms: 0, end_ms: 1, n: c.reduce((a, b) => a + b, 0), columns: 1, lo, hi, c,
});

describe("distribution", () => {
  const a = w([null, 0.1, 1], [0.1, 1, null], [6, 3, 1]);

  it("ecdf is exact at bucket edges and a box inside each bucket", () => {
    expect(ecdf(a)).toEqual([
      { lo: null, hi: 0.1, f0: 0, f1: 0.6 },
      { lo: 0.1, hi: 1, f0: 0.6, f1: 0.9 },
      { lo: 1, hi: null, f0: 0.9, f1: 1 },
    ]);
  });

  it("density is share per decade; open buckets have none", () => {
    const d = bars(a, "density");
    expect(d[0].y).toBeNull();
    expect(d[1].y).toBeCloseTo(0.3);
    expect(bars(a, "count").map((b) => b.y)).toEqual([6, 3, 1]);
    expect(bars(a, "share").map((b) => b.y)).toEqual([0.6, 0.3, 0.1]);
  });

  it("max ECDF gap uses only edges where both ECDFs are exact", () => {
    const b = w([null, 0.1, 1], [0.1, 1, null], [2, 6, 2]);
    const g = maxEcdfGapAtEdges(a, b)!;
    expect(g.at).toBe(0.1);
    expect(g.gap).toBeCloseTo(0.4);
    const coarse = w([0.05], [0.5], [10]); // 0.1 lies inside (0.05, 0.5]: unknown there
    expect(maxEcdfGapAtEdges(a, coarse)?.at).not.toBe(0.1);
  });

  it("empty windows have no ECDF", () => {
    expect(ecdf(w([], [], []))).toEqual([]);
    expect(maxEcdfGapAtEdges(a, w([], [], []))).toBeNull();
  });
});
```

The share values (0.6 / 0.3 / 0.1) come from c/total with total = 10, which are exact divisions here.

- [ ] **Step 2: Implement**

```ts
// ui/src/chart/distribution.ts
import type { WindowHist } from "../lib/api";

export type BarMode = "count" | "share" | "density";
export interface Bar { lo: number | null; hi: number | null; y: number | null }
export interface EcdfStep { lo: number | null; hi: number | null; f0: number; f1: number }

const total = (w: WindowHist) => w.c.reduce((a, b) => a + b, 0);
const order = (w: WindowHist) =>
  w.c.map((_, i) => i).sort((i, j) => (w.hi[i] ?? Infinity) - (w.hi[j] ?? Infinity) || (w.lo[i] ?? -Infinity) - (w.lo[j] ?? -Infinity));

/** Bars on the source buckets (never finer). density = share per decade of value, so
 * unequal buckets compare fairly on a log axis; open/non-positive buckets have no density. */
export function bars(w: WindowHist, mode: BarMode): Bar[] {
  const t = total(w);
  return order(w).map((i) => {
    const lo = w.lo[i], hi = w.hi[i], c = w.c[i];
    if (mode === "count") return { lo, hi, y: c };
    const share = t > 0 ? c / t : 0;
    if (mode === "share") return { lo, hi, y: share };
    return { lo, hi, y: lo !== null && hi !== null && lo > 0 && hi > lo ? share / Math.log10(hi / lo) : null };
  });
}

/** Exact at bucket edges: F(hi) = cumulative/total. Inside a bucket F is only known to lie
 * in [F(lo), F(hi)], drawn as a box, never interpolated (spec §5.1). */
export function ecdf(w: WindowHist): EcdfStep[] {
  const t = total(w);
  if (!(t > 0)) return [];
  let acc = 0;
  return order(w).map((i) => {
    const f0 = acc / t;
    acc += w.c[i];
    return { lo: w.lo[i], hi: w.hi[i], f0, f1: acc / t };
  });
}

/** Largest |Fa(x) - Fb(x)| over bucket edges x where both are exact (x not strictly inside
 * a non-empty bucket of either). A lower bound on the KS distance, not a test. */
export function maxEcdfGapAtEdges(a: WindowHist, b: WindowHist): { gap: number; at: number } | null {
  const ta = total(a), tb = total(b);
  if (!(ta > 0 && tb > 0)) return null;
  const edges = new Set<number>();
  for (const w of [a, b])
    w.c.forEach((_, i) => {
      if (w.lo[i] !== null) edges.add(w.lo[i] as number);
      if (w.hi[i] !== null) edges.add(w.hi[i] as number);
    });
  const exact = (w: WindowHist, t: number, x: number): number | null => {
    let below = 0;
    for (let i = 0; i < w.c.length; i++) {
      const lo = w.lo[i] ?? -Infinity, hi = w.hi[i] ?? Infinity;
      if (lo < x && x < hi) return null;
      if (hi <= x) below += w.c[i];
    }
    return below / t;
  };
  let best: { gap: number; at: number } | null = null;
  for (const x of [...edges].sort((p, q) => p - q)) {
    const fa = exact(a, ta, x), fb = exact(b, tb, x);
    if (fa === null || fb === null) continue;
    const gap = Math.abs(fa - fb);
    if (!best || gap > best.gap) best = { gap, at: x };
  }
  return best;
}
```

- [ ] **Step 3: Run** `just ui-test`
- [ ] **Step 4: Commit**: `feat(ui): source-bucket bars, edge-exact ECDF and the ECDF gap at bucket edges (4ok.4)`

---

### Task 15: `DistributionPlot`, "Distribution here", compare, e2e spec

**Files:**
- Create: `ui/src/components/DistributionPlot.svelte`, `ui/e2e/distribution.spec.ts`
- Modify: `ui/src/components/SelectionMenu.svelte`, `ui/src/Panel.svelte`

- [ ] **Step 1: SelectionMenu.** Add an optional prop `distribution = false` and an action button in `mode === "actions"`:

```svelte
    {#if distribution}
      <button type="button" disabled={busy} onclick={showDistribution}>Distribution here</button>
    {/if}
```

with `const showDistribution = () => run(postJSON(`/api/panels/${panelId}/distribution`, { start_ms: startMs, end_ms: endMs, baseline: "previous" }));`. The new panel arrives through the existing `panel.created` event stream.

- [ ] **Step 2: DistributionPlot.svelte.**
- Props: `{ data: HistogramPanelData; series; width; unit; onRendered(ms, points) }`. Canvas class `distribution`, height 220 per facet.
- x: `valueAxis(allLo, allHi, plotW)` across all windows (log for latency, with open-bucket strips at the ends).
- Local state `view: "histogram" | "ecdf"` defaults to `data.mark`. `mode: BarMode` defaults to `"count"` with one window and `"density"` otherwise; `"count"` is disabled with ≥ 2 windows and a tooltip "windows have different n".
- Histogram view:
  - Per window, an outline step path in `PALETTE[k]`; translucent fill only for the first window.
  - Bars span `[axis.pos(lo), axis.pos(hi)]`; open buckets occupy their strip.
  - y is linear from 0 (bars include zero, spec §6.2).
  - Bucket edges are drawn as small ticks on the x axis.
- ECDF view, per window:
  - boxes `[pos(lo), pos(hi)] × [f0, f1]` filled at 15% alpha;
  - horizontal segments at `f1` from `hi` to the next `lo` (exact);
  - dots at `(hi, f1)` (exact).
  - Y from 0 to 1.
- Legend (`.legend`), per window: `${label} ${fmtRange(start_ms, end_ms)} · n = ${n.toPrecision(4)} (${columns} steps)`, plus `· low n` when n < n_min. With 2 windows add `max |ΔF| at bucket edges = ${gap.toFixed(2)} at ${fmtValue(at, unit)}`.
- Tooltip on hover: bucket `(lo, hi]`, count, share for each window.
- Points for the render report: total bars across windows.
- Panel.svelte: add a `{:else if data && data.kind === "histogram"}` branch rendering one `DistributionPlot` per series (≤ 12). Report render with `height_px = 220 × facets`.

- [ ] **Step 3: E2E spec** (written, **not run**; the user runs `just e2e`):

```ts
// ui/e2e/distribution.spec.ts
import { expect, test } from "@playwright/test";
import { brushMiddleThird } from "./helpers.js";

test("histogram heatmap within budget; brush opens the distribution with n", async ({ page, request }) => {
  const q = await request.post("/api/query-distribution", {
    data: { selector: "tn_demo_request_duration_seconds_bucket", by: ["instance"], start: "now-3h", end: "now-10m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset, summary } = await q.json();
  expect(summary.representation).toBe("distribution");
  const s = await request.post("/api/show", { data: { dataset, question: "How is demo latency distributed over time?", unit: "s" } });
  const { panel } = await s.json();
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas.heatmap")).toHaveCount(3);
  await expect(el).toHaveAttribute("data-budget-exceeded", "false");

  await brushMiddleThird(page, panel.id);
  await page.locator(".selection-menu").getByRole("button", { name: "Distribution here" }).click();
  const hist = page.locator("section.panel", { hasText: "distributed between" });
  await expect(hist.locator("canvas.distribution").first()).toBeVisible();
  await expect(hist.locator(".legend").first()).toContainText("n =");
  await expect(hist.locator(".legend").first()).toContainText("previous");
});
```

- [ ] **Step 4: Run** `just ui-test && just ui-check && just ui-build && just test`. Then do a manual check in the dev UI: brush a heatmap and confirm the new histogram panel shows selection vs previous; toggle to ECDF.
- [ ] **Step 5: Commit**: `feat(ui): histogram/ECDF panels with window comparison and "Distribution here" (4ok.4)`

---

## Phase D: polish

### Task 16: Quantile-bucket overlay on heatmaps (gated by n)

- Add `quantileCells(series: HeatSeries, q: number): { ts: number; lo: number | null; hi: number | null }[]` in `heatmap.ts`.
  - Per column, walk the cells sorted by `hi` and pick the bucket where the cumulative share reaches q.
  - Only emit columns with `n >= minSamples(q)`, where `minSamples(q) = Math.ceil(10 / (1 - q))` (port with a test: p50 → 20, p95 → 200, p99 → 1000).
- Draw a 1 px white-and-black outlined rectangle around that bucket (not a line through an interpolated value). Legend: `p95 bucket (only where n ≥ 200)`.
- Toggle in the heatmap legend: none / p50 / p95 / p99.
- Tests:
  - a column with n = 150 has no p95 cell;
  - a column with n = 300 returns the bucket containing the 95th share;
  - an open top bucket returns `hi: null`.
- Commit: `feat(ui): quantile-bucket overlay on heatmaps only where n is enough (4ok.5)`

### Task 17: Toggles, guidance, live Play check, close beads

- **Heatmap legend controls.** Colour `count` / `density` (share of the column) and colormap `viridis` / `cividis`. These are local UI state; persisting them is not needed for the MVP.
- **Histogram-as-lines hint.** In `exprkind`, add `looks_like_histogram(expr)`: true when a non-quantile expression mentions `_bucket`, groups `by (… le …)`, or mentions `vmrange`. `query` then adds the caveat `histogram_as_lines`; `panelNotes` text: "This looks like histogram buckets drawn as lines; use query_distribution for a heatmap of counts". Test both.
- **Claude guidance.** Add to `INSTRUCTIONS`: "Histograms first: for latency, `query_distribution` the histogram and `show` it (heatmap); use `show(mark="histogram", windows=[spike, baseline])` to compare windows and cite n per window. Quantiles only on request, and summaries give them as the bucket containing them." Update `tests/unit/test_mcp_instructions.py` if it pins the content.
- **Live check on Grafana Play** (polite, through the daemon after one restart):
  1. `query_distribution(selector='traces_spanmetrics_latency{service="checkoutservice",span_kind="SPAN_KIND_SERVER"}', by=["cloud_region"], start="2026-10-01T09:50:00Z", end="2026-10-01T10:20:00Z", step="1m", source="play")`. Expect a native schema 3 description, 3 series, ap-south-1 `missing_columns > 0`, and `low_count` (the 10:02 column, n ≈ 8).
  2. `show(dataset, "Did checkout latency split into fast and very slow requests during the burst?", unit="s")`. Expect three heatmap facets: burst columns dense near 0.3–1 s; the 10:02 ap-south-1 column a dimmed cluster at 14.7–34.9 s.
  3. In the UI, brush ap-south-1 around 10:02 and click "Distribution here". Expect a histogram with n per window, selection vs previous.
- Run everything: `just test && just lint && just ui-test && just ui-check && just ui-build && uv run pytest -m integration -q`.
- Commit: `feat: distribution toggles, histogram-as-lines hint, histograms-first guidance (4ok.4, 4ok.5)`
- Close the beads:

```bash
bd close telemetry-nerd-4ok.5 --reason "heatmap mark: viridis/cividis, log-y, no-data vs zero, low-n columns, exact LOD, render budget; Play check"
bd close telemetry-nerd-4ok.4 --reason "histogram/ECDF over windows with compare, n shown, brush follow-up; e2e spec written (run just e2e)"
```

---

## Acceptance

- Classic (VictoriaMetrics integration and Play fixture), vmrange (VictoriaMetrics integration) and native (Play fixture) histograms all become distribution datasets.
  - Per-column n equals `histogram_count(increase(...[step]))`, or the `+Inf` bucket increase for classic.
  - Columns with no data are distinct from n = 0.
- Unit tests cover:
  - `le`-cumulative conversion (monotonic fix, `missing_inf`, overflow);
  - `vmrange` parsing;
  - native schema detection;
  - time rebucketing and value merging that preserve totals (hypothesis);
  - value merging that never splits a source bucket.
- Claude's summary gives n, coverage, low-n columns, the bucket scheme, and quantile *buckets* only where n ≥ min_samples(q). It contains no percentile value and is under 2 KB.
- The heatmap renders within budget (cells ≤ area/8, < 100 ms). Viridis/cividis are monotonic in luminance. Log-y is used for latency. Hatching marks no data and dimming marks low n.
- Brushing a heatmap or a percentile panel opens a histogram panel comparing selection vs previous, with n per window. ECDF is exact at edges and boxed inside buckets.

---

## Beads breakdown (children; create with `bd create --parent <id>`, link with `bd dep add <child> <depends-on>`; check `bd create --help` for flags)

Under **telemetry-nerd-4ok.3 (distribution datasets)**:

| Bead | Task | Depends on |
|---|---|---|
| 4ok.3.1 Distribution model + classic `le` conversion | 1 | — |
| 4ok.3.2 `fetch_histogram` (classic) + native-histogram error in `query` | 2 | 3.1 |
| 4ok.3.3 Distribution storage (dist_rows/dist_columns) | 3 | 3.1 |
| 4ok.3.4 `query_distribution` service, summary, MCP, HTTP | 4 | 3.2, 3.3 |
| 4ok.3.5 Demo histogram + VictoriaMetrics integration (classic) | 8 | 3.4, 4ok.5.3 |
| 4ok.3.6 Native histograms (parse, schema, schema-lowering LOD) | 9 | 3.4, 4ok.5.1 |
| 4ok.3.7 VictoriaMetrics vmrange | 10 | 3.4, 4ok.5.1 |
| 4ok.3.8 Play preset + recorded fixtures + replay tests | 11 | 3.6 |
| 4ok.3.9 (P2, optional) `histogram_over_time` of gauges (`of="gauge"`, VictoriaMetrics) | — | 3.7 |
| 4ok.3.10 (P2) Cache distribution chunks in SeriesCache | — | 3.4 |

Under **telemetry-nerd-4ok.5 (heatmap)**:

| Bead | Task | Depends on |
|---|---|---|
| 4ok.5.1 Distribution LOD + heatmap panel_data + marks/validator + render budget | 5 | 4ok.3.4 |
| 4ok.5.2 UI colormap, value axis, heatmap layout (pure) | 6 | 5.1 |
| 4ok.5.3 HeatmapPlot canvas + Panel wiring + notes | 7 | 5.2 |
| 4ok.5.4 Quantile-bucket overlay (n-gated) | 16 | 5.3 |
| 4ok.5.5 Toggles, guidance, live Play check | 17 | 5.3, 4ok.3.8 |

Under **telemetry-nerd-4ok.4 (histogram chart)**:

| Bead | Task | Depends on |
|---|---|---|
| 4ok.4.1 `histogram_source` for quantile datasets | 12 | 4ok.3.4 |
| 4ok.4.2 Window histograms, histogram/ECDF panels, `show(mark, windows)`, follow-up endpoint | 13 | 4.1, 4ok.5.1 |
| 4ok.4.3 UI bars / ECDF / edge gap (pure) | 14 | 4ok.5.2 |
| 4ok.4.4 DistributionPlot + "Distribution here" + compare + e2e spec | 15 | 4.2, 4.3, 4ok.5.3 |

Critical path for the vertical slice: 3.1 → 3.2 / 3.3 → 3.4 → 5.1 → 5.2 → 5.3 → 3.5.

---

## Risks and decisions for the user

1. **Bead 4ok.4's acceptance text ("13 requests split fast vs 10–50 s") does not match the data at a 1 m step.** On Play, 10:02Z ap-south-1 has n ≈ 8, all in 14.7–34.9 s. The 13 (and its fast requests) came from a wider rate window. Recommendation: rewrite the acceptance as "n equals histogram_count(increase()) for the selected window, and the slow cluster sits in 14.7–34.9 s buckets". The user should confirm.
2. **Fractional counts.** Prometheus `increase()` extrapolates. We store float counts and caveat `estimated_counts` instead of rounding, because rounding per bucket breaks additivity. Alternative: integer rounding per column, which is not recommended.
3. **New MCP tool vs auto-detection.** I recommend `query_distribution(selector, by)`. Auto-detecting in `query` would let Claude choose rate windows that overlap steps and would blur the "never from quantiles" rule. Please confirm the tool name and parameter shape.
4. **Step floor 2 × scrape interval.** At 20 s resolution on Play, distributions cannot go below 40 s steps. That is more conservative than needed on VictoriaMetrics.
5. **Low-n threshold 20** (p50 rule) for dimming heatmap columns and flagging windows. Lower values (e.g. 10) show more shape with more noise.
6. **No caching for distribution queries in MVP.** Every `query_distribution` and every "Distribution here" on a percentile panel issues a source query (polite gate applies). Bead 4ok.3.10 adds caching.
7. **Mixed `le` sets across series** use the intersection for the scheme and value LOD. Series with extra edges keep their finer buckets until LOD merges them.
8. **Native negative buckets and NHCB.** Negative buckets go to the low strip on log axes and are not merged. NHCB is treated as `custom` edges; it is untested against a live source.
9. **Grafana Play retention.** Fixtures must be recorded soon (Task 11) while 2026-10-01 data is still available. Once committed they are permanent.
10. **Concurrent work on `Panel.svelte`** (3fs.5 highlights just added `PinButton`). Keep the heatmap and histogram branches additive. If `Panel.svelte` grows further, a later refactor into `TimeSeriesPlot.svelte` is worth a bead.
11. **E2E** specs are written but not run by the implementer (per instructions). The 4ok.4 and 4ok.5 acceptance "e2e test passes" needs a user `just e2e` run before the beads close; alternatively, close them on unit/integration evidence and keep a follow-up.

### Critical Files for Implementation
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/analysis/histogram.py (new; parsers and converters)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/core/service.py (query_distribution, show, panel_data, distribution_panel)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/datasets/store.py (DatasetMeta fields, distribution storage)
- /Users/avishai/code/telemetry-nerd/src/telemetry_nerd/sources/promql.py (fetch_histogram, native-histogram guard)
- /Users/avishai/code/telemetry-nerd/ui/src/Panel.svelte (plus new ui/src/chart/heatmap.ts and ui/src/components/HeatmapPlot.svelte)