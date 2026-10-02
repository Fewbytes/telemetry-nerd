# Telemetry Nerd — Graphing Guide

How Telemetry Nerd draws telemetry. Distilled from two research reports
(`research-docs/telemetry-graphing-style-guide - claude.md`, cited here as **[C]**, and
`research-docs/Telemetry Data UX Design Guide - gemini research.md`, cited as **[G]**),
filtered through this project's principles (MVP spec §1.2, §6) and checked against the
current UI code.

This is not a dashboard guide. Telemetry Nerd is an **investigation workspace**: every
panel answers one explicit question, every claim is scoped and evidence-backed, and
*correct beats conventional* (spec §1.2.3). Rules that only make sense for NOC wallboards
are dropped (see §9).

Evidence tags (from [C]): **E** controlled experiment · **P** practitioner/spec, read
first-hand · **P2** secondhand · **X** inference, treat as hypothesis · **TN** project
decision (our own rule, justified in the spec).

---

## 1. How the two sources compare

| | [C] Claude | [G] Gemini |
|---|---|---|
| Sources | 15, mostly primary (CHI/TVCG/VLDB papers, Few, Wilke, SRE book) | ~98, mostly vendor blogs, Medium, SEO listicles; few primaries |
| Claim discipline | Every rule tagged E/P/P2/X, conditions stated, conflicts and gaps listed | Uniform confident prose; experimental and anecdotal claims indistinguishable |
| Scope | Graph display only | Dashboards, IA, OTel, alerting, a11y, perf |
| Best for | *What is true* about perception, tails, heatmaps, smoothing | *What to build* around the chart: exemplars, heatmap engineering, live update, a11y |

**Use [C] as the authority on perception and statistics; mine [G] for engineering
patterns and gaps [C] scoped out.** Where they disagree, [C] wins unless the [G] point
cites a primary source (M4 paper, Datadog engineering blog, W3C).

### [G] claims rejected or downgraded

| [G] claim | Problem | Our position |
|---|---|---|
| Horizon graphs let you watch "dozens" of series; magnitude by saturation | Ignores [C] H2: >2 bands raise error and time (E); needs training | Not a default form. If ever added: ≤2 bands, opt-in. Many-series case uses group band / series heatmap (spec §6.3) |
| LTTB "preserves the sharp, transient anomalies" | LTTB is sampling; it keeps one point per bucket and can drop extremes. Only M4/min-max is exact for line rasterization | Server-side min/max-preserving aggregation (spec §6.5). Never LTTB for evidence views |
| Percentile lines (p50/p90/p99) are the histogram visualization | Fine as an overlay, wrong as the only view; [C] P4: cut-off at p95 misleads | Heatmap first; percentile curves / CCDF for tails |
| Truncated y-axis = high Lie Factor, so anchor at zero | Line charts may legitimately omit zero ([C] A3, Wilke); forcing zero hides real shifts | Contextual y-range (spec §6.2): `reference` default, `data` zoom always badged + context strip + "y ≠ 0" marker |
| "Eye-tracking confirms" F/Z scan; top-left = KPI | Secondary blog source; dashboard-specific | Irrelevant: panels are a question-driven stream, not a grid |
| UI must cap at 10,000 series | Arbitrary number from a vendor blog | Our budget is perceptual, not DOM-based: 5 lines / 12 facets / group band beyond (§3) |
| Cleveland–McGill table puts "small multiples" at rank 2, heatmaps last | Rank list is a paraphrase; [C] notes Cleveland not read first-hand, the E evidence is Heer & Bostock replication | Position on common scale first ([C] rule 1, E). Heatmaps are for *distribution shape*, not value reading |
| Smooth x-axis scrolling animation for live data | No evidence; [C] T5: animation worse than static for analysis (E, secondhand) | Discrete updates at ~1 Hz max, y-axis stable (§7) |

### [G] points adopted

- **M4** (Jugel et al., VLDB 2014): first/last/min/max per pixel column → exact line
  raster. Already our §6.5 design.
- **Datadog heatmap engineering**: linear colour washes out rare modes; pure
  rank/percentile colour flattens the main mode; blend the two. Red avoided in density
  ramps. `setImageData` past ~4 px/bin; coarse hover grid to avoid jitter.
- **Exemplars**: trace-ID markers on metric points/heatmap cells → drill to trace. Future
  (traces are post-MVP), but reserve the seam.
- **Dual-axis alternatives**: small multiples with shared x (have), or **indexed** chart
  (both series as ratio to a common baseline). Combine with [C] A8: ratios on log axis, 1
  in the middle.
- **Y-axis stability under live updates**: rescale only on breach, eased.
- **Anomaly/normal band**: shaded expected envelope; breaches marked with a non-colour
  cue. Matches spec §6.2 seasonal normal band.
- **Non-interruptive alerting**: modal pop-ups get blindly dismissed (CDS research).
  Highlights stay in-panel, ephemeral (already our design).
- **Accessibility**: canvas is opaque to screen readers; provide a data-table view and
  ARIA labels. Gap for us (§8).
- **Instrument semantics drive the view** (OTel counter → rate, up/down counter → level,
  gauge → level/bullet, histogram → heatmap). Already the catalog's job (2as.4 T0 rules).
- **Meta-analytics**: the UI event log (ambient events) can tell which views are used and
  which drill-downs are missing. Cheap to exploit later.

---

## 2. Core rules

Hard rules are enforced by the validator (`src/telemetry_nerd/charts/spec.py`) as
errors; overriding needs `override: {rule, reason}` and renders a caveat (spec §6.3).

1. **Quantities by position on a common scale.** [E, C rule 1]
2. **Every panel answers one question**, shown in its header. [TN §1.2.5]
3. **Mean never alone for latency-like data.** Distribution or tail must be visible. [P, C rule 10, D2]
4. **Never average percentiles.** Not across time, not across series, not in captions. [P2, C P8; TN]
5. **Downsampling must preserve extremes.** Mean line + min/max envelope; never
   peak-eroding averages or LTTB for evidence views. [P, C R5; M4 via G]
6. **Gaps stay gaps.** No interpolation, no zero-fill; "no data" ≠ 0. [P2, C G1–G4; TN] (`spanGaps: false` everywhere in `toUplot.ts`.)
7. **No dual y-axes.** Small multiples with shared x, or an indexed (ratio) chart. [TN; G]
8. **No stacking of non-additive quantities** (gauges, ratios, percentiles). Stacked
   areas also hurt individual-series reading even when additive. [TN; E2, C §2]
9. **Units on every axis**, with provenance (explicit / name-suffix / unknown — shown
   honestly). [TN]
10. **Colour carries meaning, never alone.** Pair with position, shape, label or order. [P, C rule 8]
11. **Uncertainty and aggregation travel with the number** (count, n_min, CIs, step,
    representation in the provenance footer). [TN §1.2.4]

---

## 3. Choosing the form

The catalog's auto-chart (`show(signal)`) picks the form; explicit specs override.

| Question / data | Form (mark) | Avoid | Basis |
|---|---|---|---|
| Trend of one gauge/rate | `line+envelope` (mean + min/max band) | Mean only | TN, C R5 |
| Counter | rate with envelope; never raw cumulative value | Raw counter line | P, G (OTel) |
| Latency/size distribution over time | `heatmap` (x time, y value, colour count) | avg/max lines alone | P, C D1–D2 |
| Tail at a window ("how bad is the worst 0.1%?") | `ccdf` / `quantile_curve`, tail axis stretched | Percentiles cut at p95 | P2, C P4–P5 |
| Shape comparison now vs reference | `histogram` / `ecdf` with ≤4 windows | Overlaid density with hidden bin choice | P, C D12 |
| Spans >2 decades | log axis, labelled with variable+unit (not "log(x)") | Linear axis with bulk in a corner | P, C A7; TN §6.2 |
| Ratio vs baseline (p99 now / last week) | log axis, 1 centred | Linear ratio axis | P, C A8 |
| Sustained-shift detection in noise | smoothed line **over** raw/envelope, labelled + window | Smoothing alone | E, C R1–R4, R7 |
| Isolated-spike detection | raw / min-max / heatmap | Any smoothing | P, C R3 |
| Error ratio | Wilson band; normalized comparison → funnel plot (P2 phase) | Bare ratio line | TN |
| Success vs failed latency | **separate series** | One merged latency | P, C T7 |
| Saturation | most-constrained resource + its limit line; short-window p99 as early signal | Average utilization | P, C T8 |

**Series budget** (spec §6.3; consistent with [C] C4/M5/M7 — 3–5 categorical colours,
readability collapses past ~8):

- ≤5 series → lines in one plot (shared-space wins for **local** comparisons — E, C M1).
- 6–12 → small multiples, **identical y-scale** (split-space wins for **dispersed** comparisons — E, C M1, M6).
- >12 → group band (median + MAD) with only outliers drawn and labelled, or series heatmap (row per series). [P, C M5]
- Horizon graphs: not offered. If added, ≤2 bands, opt-in, with a legend explaining layering. [E, C H2]

---

## 4. Axes and y-range

Implemented in `ui/src/chart/yview.ts`, `axis.ts`, `charts/yview.py`.

- **Contextual range** (spec §6.2): `reference` default; `data` zoom always labelled
  with badge + context strip; `semantic` = natural bounds. Never below a natural lower
  bound. Lines may omit zero but show the "y ≠ 0" marker. [TN; C A3 supports]
- Bars include zero; bars on log axis encode ratios and start at 1. Amounts on a log axis
  → dots, not bars. [P, C A1–A2]
- Fills under a line only with a zero baseline. Envelope bands are fills *between* two
  series, which is fine. [P, C rule 3]
- Gridlines: sparse, ≥8 px apart, light. [E, C A4–A5]
- **Live data**: y-range does not follow every new point; rescale only when a point
  breaches the current view, and say so. [G; X]

---

## 5. Distributions and heatmaps

Implemented in `ui/src/chart/heatmap.ts`, `colormap.ts`, `HeatmapPlot.svelte`.

- Colormaps: perceptually uniform only (viridis, cividis). No rainbow/jet, no red in
  density ramps. [P, C C1; G Datadog]
- Rows: log-spaced when the value range spans decades (auto log ≥2 decades). Open-ended
  buckets (`(-Inf, e]`, `(e, +Inf)`) get a fixed strip, never a fake range. [P, C P2/D14; TN]
- Cells must hold several events; low-n cells are faded and flagged (`lowN`, `n_min`).
  [P, C D3; TN]
- **Colour mapping is a deliberate distortion — say which.** Current count mapping is
  `log1p(v)/log1p(max)`; density is linear. Datadog's linear/rank blend is the alternative
  if rare modes still vanish. The legend must state the mapping. [P, C D4; G] → **gap** (§10).
- Outlier trimming (e.g. top 0.1%) only with visible disclosure. [P, C D6]
- Keep finer data for re-render on zoom; state the resolution shown (step in footer). [P, C D7]
- Percentile overlays on heatmaps: computed from buckets with bucket-edge bounds, gated
  by `minSamples(q) = ⌈10/(1−q)⌉`. [TN]
- Separate read/write (or success/failure) distributions; don't merge modes. [P, C D9, T7]
- Hover on a coarse grid (one time bucket), not per pixel. [G]

**Tails.** A p95 cutoff misrepresents user experience: with ~200 independent requests per
session, ~99.995th percentile per request is needed to cover 99% of sessions [P2, C P4].
Prefer CCDF / quantile curve with the tail stretched, plot the max (it's an observation,
not a statistic), and draw requirement lines on it. [P2, C P5–P7]

### Threshold fractions beat percentiles (Hartmann, SREcon19 EMEA, "Latency SLOs Done Right" — [H])

Slides read in full; Circonus data-scientist talk, practitioner evidence (P) with one
worked numeric example.

- **The question to answer is "what fraction of requests in period T were ≤ X ms?"**,
  not "what was p99". It aggregates exactly across time, nodes and endpoints (sum good,
  sum total), and the user can move X freely if the data is a histogram. [P, H]
- **Averaged percentiles are badly wrong in practice**: 24 hourly p90s averaged = 60.3 ms;
  true p90 of the merged 811k requests = 35.8 ms (68.5% error). Cause: low-traffic hours
  (25–600 req, wide spread) weigh the same as peak hours (35–98k req). [P, H] Use as the
  canonical caveat text and as a regression fixture.
- **A percentile-over-time line gives every time bucket equal visual weight regardless
  of request count.** His example: a p99 spike on a log-scale chart that looks like the
  incident, while 99.5% of all requests happened elsewhere. → Percentile and heatmap views
  need traffic context: count strip aligned under the plot, low-n buckets faded (we fade
  below `n_min`; a count strip is missing). [P, H; remedy X]
- **Threshold readout UI**: histogram over the window with a draggable threshold showing
  three numbers — *below / inside the straddled bucket / above* (his demo: 89.3% / 0.2% /
  10.5%). This is exactly our bucket-edge uncertainty made visible: the middle number is
  the bound width. [P, H]
- **Classic Prometheus histograms are threshold counters**: exact only at `le` edges.
  Thresholds between edges must report a [lo, hi] interval, never an interpolated point.
  [P, H; TN]
- **Pre-computed quantiles (Prometheus `summary` `quantile=` series, exported p99
  gauges) cannot be aggregated** across instances or time. Catalog should mark them
  non-aggregatable; validator should reject `avg/sum/max_over_time` over them and any
  multi-series merge, with an override caveat. [P, H; TN rule 4]
- Mergeable sketches (HDR, circllhist log-linear 2-significant-digit bins, t-digest,
  DDSketch) all merge with ≤0.3% p90 error in his benchmark. Native/exponential
  histograms belong to this family: rows should be log-linear, and the heatmap is the
  natural render ("HDR histogram metric" slide = heatmap + marginal histogram inset,
  same as our panel anatomy). [P, H]

### Statistics for Engineers workshop (Hartmann, github.com/HeinrichHartmann/Statistics-for-Engineers — [SfE])

Notebooks read: 2019 SREcon Dublin (1.2–2.5), 2018 Düsseldorf aggregation, 2017 queuing,
2022 sampling. Practitioner teaching material (P), with runnable demos on real latency data.

**Mergeable vs robust: the table the validator should encode.** [P, SfE]

| statistic | mergeable | robust | aggregate across time/series? |
|---|---|---|---|
| count, count_below(X) | yes | yes | sum |
| ratio_below(X) | only with total count | yes | ratio of sums, never mean of ratios |
| mean | only with count | no | count-weighted |
| min / max | yes | no | min / max |
| median, percentile, truncated mean, MAD, IQR | **no** | yes | **forbidden** — recompute from merged histogram/raw |
| histogram (fixed bins) | yes (same bin layout) | — | bucket-wise sum |

"You have to choose between robust and mergeable" — histograms escape the dilemma, which
is why distribution views are our default for latency.

**Downsampling artifacts** (1.3 "Pitfalls in Reading Graphs"). [P, SfE]
- *Spike erosion*: mean-aggregation washes isolated spikes out; skip-sampling drops them
  at random. → our min/max envelope.
- *Beam effect*: skip-sampling oscillating data aliases to a false, slower period; mean
  aggregation low-passes it away. Periods below 2×step are not real in any downsampled
  view. → Spectrum op must work at scrape resolution or cut at Nyquist (2×step) and say so.
- Verified in code: `PromQLSource.build_queries` uses `*_over_time` / `rollup` over the full
  step window, so the main path is aggregation, not skip-sampling. Good.
- "Interpolation lines add the illusion of continuity." Each plotted bucket value
  represents an interval; a stepped path states that honestly. [P, SfE; open question
  whether to default to steps]

**Percentile definitions differ.** Hyndman–Fan list 9 types; software mostly uses type 7
(interpolated), probability theory type 1. Histogram-derived quantiles are a *range* (bucket
edges). → Provenance footer should state the quantile estimator ("bucket-edge bounds" /
"histogram_quantile linear interpolation"). [P, SfE]

**Deviation & outliers.** "68/95/99.7 within kσ" holds only for normal data — never for
latency. Use MAD or IQR, Tukey fences (k·IQR beyond quartiles); boxplots show median,
quartiles, outliers as points. Supports our group band = median + MAD. Caveat: MAD/IQR
aren't mergeable, compute on the merged data. [P, SfE]

**Survival curve.** ratio_above(threshold) vs threshold, one curve per hour overlaid:
"the faster it goes to 0 the better". = our `ccdf` mark; per-window overlays are the
comparison form. Multi-threshold counts over time ("banded metrics", VividCortex) = count
over threshold over time with several thresholds. [P, SfE]

**Histograms.** Bin choice changes the look (0.1/1/10/100 demo); data-dependent bin rules
(√n, Scott, Freedman–Diaconis) break mergeability. Org-wide fixed layout, or log-linear
(fixed relative error, human-readable edges). Variable-width bins must be drawn as
density (count / width), not count — we do per-decade density in `distribution.ts`.
**Classic histograms with different `le` sets cannot be summed by `le`.** [P, SfE]

**Sampling.** Subsampling keeps mean/p50, systematically underestimates max, and gives
large relative error on p99/p99.9; relative error of a sampled count ≈ √((1−p)/(pN)).
Bootstrap gives CIs for any statistic (tier-2). [P, SfE]

**Queuing (2017).** Little's law L = λW; residence time W = S/(1−ρ) (M/M/1); service-time
variance raises waits (Pollaczek–Khinchine); USL for throughput vs concurrency.
Utilization over an interval = discrete derivative of busy-time integral (a counter) — so
utilization is a *rate of a counter*, aggregate accordingly. Latency vs utilization
`xy` plot with the fitted hockey-stick is the natural capacity view (`fit` mark). [P, SfE]

**Smoothing in disguise.** Unix load average = exponential smoothing. Label such
signals as smoothed (window/α) in the metric card. [P, SfE]

---

## 6. Colour and contrast

- Categorical: Okabe–Ito subset (`PALETTE` in `toUplot.ts`), ≤5 per plot. [P, C C5]
- Status ranges: intensity steps of one hue, not distinct hues. [P, C C9]
- Diverging: no red–green; use blue-content-differing pairs. [P, C C2]
- Large fills: light tint, darker same-hue line. Envelope band at α≈0.35 is OK because
  values are redundantly in the legend. [P, C C7; C C11 exemption]
- **Contrast floor: 3:1 against background for marks needed to read the chart** (WCAG
  SC 1.4.11). Measured for the current palette:

  | colour | on light `#ffffff` | on dark `#16181d` |
  |---|---|---|
  | `#0072B2` | 5.19 | 3.42 |
  | `#E69F00` | **2.25** | 7.88 |
  | `#009E73` | 3.42 | 5.19 |
  | `#CC79A7` | 3.06 | 5.80 |
  | `#56B4E9` | **2.31** | 7.70 |

  Orange and sky blue fail on the light theme, worse once a 1.5 px line anti-aliases.
  Fix with theme-specific darker variants (keep hue order). → **gap** (§10).
- Dark-mode perception is untested in every source; 3:1 is a floor, not proof. [X, C C13]
- Test with a CVD simulator, especially thin lines. [P, C C6]

---

## 7. Rendering, resolution, live updates

- Points ≤ pixels; target ≤2 points/px and ≤100 ms render; breach logs
  `render_budget_exceeded` and falls back coarser (spec §6.5). [P, C R5; TN]
- Server sends ~1 bucket/px with first/last/min/max semantics (M4). [P, G/M4]
- Isolated single samples between gaps must be visible (point marker). [X, C G3] → verify.
- Refresh at human-perceptible rates (~1 Hz), not per point; no continuous scroll
  animation. [P, C R6; E2 C T5]
- Smoothing, when used: label "smoothed (window W)" and keep raw/envelope underneath. [X, C R7; P2 C R4]
- Synced crosshair and time range across panels (Gestalt common fate). [TN; G]

Sizing (all from low-DPI 2009–10 studies; CSS-px mapping on HiDPI unverified):
line chart accuracy plateaus at ~80 px tall (40 px worse) [E, C A6]; small-multiple rows
down to ~24 px for single-point comparisons [E, C S1]; gridlines ≥8 px apart [E, C A4].
Treat these as floors for facet height.

---

## 8. Interaction and accessibility

- Overview → zoom/filter → details on demand; relate, history, extract (Shneiderman). [P, C I1–I2]
  Map: shared time range (relate), event log + undo (history), export view/spec + data (extract).
- Filters update in <100 ms. [P, C I5]
- Highlights / anomaly cues stay in-panel and non-modal; reserve interruption for nothing
  in MVP. [G, CDS research]
- **Canvas charts are invisible to assistive tech.** Each panel needs: an accessible name
  (the panel question), `role="img"` + description (metric, scope, range, unit), and a
  "view as table" toggle giving the plotted buckets (incl. min/max/count). [G, W3C] → **gap**.
- Annotations / deploy markers aligned on shared time axis ("what else changed then?").
  [P, C B8 — effectiveness unstudied]

---

## 9. Deliberately out of scope

From [G], dashboard-centric and not applicable to an investigation workspace:
inverted-pyramid / 12-column KPI layout, F-pattern placement, 5-second rule, dashboard
sprawl and grafonnet, radial gauges (never), bullet graphs (only if a single-value-vs-target
view is ever needed — then follow Few's spec in [C] §8), alert deduplication / blast-radius
views.

---

## 10. Gaps against current code

Found while writing this guide; candidates for beads.

1. **Light-theme contrast**: `#E69F00` and `#56B4E9` < 3:1 on white. Add per-theme
   darkened variants in `PALETTE` (via `plotColors`).
2. **Heatmap colour-mapping disclosure**: legend/footer should state `log1p count` vs
   `linear density`; consider Datadog-style linear/rank blend as an option.
3. **Accessible panels**: ARIA name/description + data-table view.
4. **Isolated samples**: confirm a lone point between nulls renders a marker in uPlot.
5. **Success vs failure split** as a catalog-driven follow-up link for latency metrics.
6. **Smoothing label** convention if/when a smoothing op lands.
7. **Exemplar seam**: reserve a `exemplars` field on distribution datasets for when
   traces arrive.
8. **Indexed (ratio-to-baseline) view** on log axis as the sanctioned dual-axis
   replacement alongside small multiples.
9. **Fraction-over-threshold op** (tier-1 + MCP tool): fraction ≤ X over window, merged
   across series, with bucket-edge [lo, hi] bounds and Wilson CI. Currently done ad hoc.
10. **Threshold readout on histogram/ECDF panels**: draggable X → below / in-bucket / above. [H]
11. **Traffic count strip** under percentile and heatmap plots. [H]
12. **Summary-quantile guard**: catalog flag + validator rule against aggregating
    pre-computed quantile series. [H] Generalize to the mergeability table. [SfE]
13. **Classic-histogram `le`-set mismatch guard**: refuse/caveat summing buckets across
    series whose bucket layouts differ. [SfE]
14. **Quantile estimator in provenance footer.** [SfE]
15. **Spectrum Nyquist cut** at 2×step. [SfE]

## 11. Open questions (no evidence either way)

Dark-mode perception; HiDPI pixel mapping of the sizing studies; effectiveness of
annotation/deploy markers; live-chart awareness (Ragan et al. 2020 unread); best
nonlinear heatmap colour mapping for exponential/native-histogram buckets.
