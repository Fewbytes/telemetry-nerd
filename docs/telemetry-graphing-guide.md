# Telemetry Nerd — Graphing Guide

How Telemetry Nerd draws telemetry. Distilled from two research reports
(`research-docs/telemetry-graphing-style-guide - claude.md`, cited here as **[C]**, and
`research-docs/Telemetry Data UX Design Guide - gemini research.md`, cited as **[G]**),
two Heinrich Hartmann sources (SREcon19 "Latency SLOs Done Right" slides **[H]**, the
Statistics-for-Engineers workshop notebooks **[SfE]**), and design decisions made while
reviewing them (**TN**). Filtered through this project's principles (`docs/principles.md`;
MVP spec §6)
and checked against the current UI code.

This is not a dashboard guide. Telemetry Nerd is an **investigation workspace**: every
panel answers one explicit question, every claim is scoped and evidence-backed, and
*correct beats conventional* (principle 3). On any conflict, `docs/principles.md` wins. Rules that only make sense for NOC wallboards
are dropped (see §9).

**We are not limited to common chart types.** Panels render on canvas. When a
conventional idiom hides the distribution, the uncertainty or the missing data, design a
better mark. For example, a group's spread is drawn as a density cloud, not as
upper/lower lines (§3, §5a). Use conventional forms only where they are actually the best
encoding. [TN]

Evidence tags (from [C]): **E** controlled experiment · **P** practitioner/spec, read
first-hand · **P2** secondhand · **X** inference, treat as hypothesis · **TN** project
decision (our own rule, justified in the spec).

Related: `docs/data-source-quirks.md` (per-backend missing-data behaviour),
`docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md` (companion
series, localized caveats, `bucket_state`).

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
| Horizon graphs let you watch "dozens" of series; magnitude by saturation | Ignores [C] H2: >2 bands raise error and time (E); needs training | Not a default form. If ever added: ≤2 bands, opt-in. The many-series case is a group density cloud + outliers (§3) |
| LTTB "preserves the sharp, transient anomalies" | LTTB is sampling; it keeps one point per bucket and can drop extremes. Only M4/min-max is exact for line rasterization | Server-side min/max-preserving aggregation (spec §6.5). Never LTTB for evidence views |
| Percentile lines (p50/p90/p99) are the histogram visualization | Fine as an overlay, wrong as the only view; [C] P4: cut-off at p95 misleads | Heatmap first; percentile curves / CCDF for tails |
| Truncated y-axis = high Lie Factor, so anchor at zero | Line charts may legitimately omit zero ([C] A3, Wilke); forcing zero hides real shifts | Contextual y-range (spec §6.2): `reference` default, `data` zoom always badged + context strip + "y ≠ 0" marker |
| "Eye-tracking confirms" F/Z scan; top-left = KPI | Secondary blog source; dashboard-specific | Irrelevant: panels are a question-driven stream, not a grid |
| UI must cap at 10,000 series | Arbitrary number from a vendor blog | Our budget is perceptual, not DOM-based: never draw many series; aggregate and draw the result (§3) |
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
2. **Every panel answers one question**, shown in its header. [TN principle 5]
3. **Mean never alone for latency-like data.** Distribution or tail must be visible. [P, C rule 10, D2]
4. **Never average percentiles.** Not across time, not across series, not in captions. [P2, C P8; TN principle 10]
5. **Downsampling must preserve extremes.** Mean line + min/max envelope; never
   peak-eroding averages or LTTB for evidence views. [P, C R5; M4 via G]
6. **Missing data is shown, not just omitted.** "No info" is information. No
   interpolation, no zero-fill; "no data" ≠ 0 ≠ "don't know". Gaps, partial buckets,
   series lifetimes and failed fetches are drawn (§5a). [P2, C G1–G4; SfE; TN principle 11]
7. **No dual y-axes.** Small multiples with shared x, or an indexed (ratio) chart. [TN; G]
8. **No stacking of non-additive quantities** (gauges, ratios, percentiles). Stacked
   areas also hurt individual-series reading even when additive. [TN; E2, C §2]
9. **Units on every axis**, with provenance (explicit / name-suffix / unknown — shown
   honestly). [TN]
10. **Colour carries meaning, never alone.** Pair with position, shape, label or order. [P, C rule 8]
11. **Uncertainty and aggregation travel with the number** (count, n_min, CIs, step,
    representation in the provenance footer). [TN principle 4]
12. **Lines are stepped by default.** Each plotted value stands for its bucket's interval;
    connecting bucket values "adds the illusion of continuity". Connected lines only on
    explicit request, with a caveat. [P, SfE; TN]
13. **Caveats are drawn where they apply, and only when present.** Every caveat has a code,
    a severity and an optional location (time spans, series, value range). A caveat with a
    location gets a visual form on the graph (rug, texture, fade, marker, badge). The
    provenance footer lists all of them, and hovering an entry highlights its location. A
    clean panel carries no caveat chrome. [TN]
14. **Only mergeable statistics are aggregated** (count, sum, min, max; mean and ratios
    only with their counts). Percentiles, medians, MAD/IQR and pre-computed quantiles are
    recomputed from merged histograms or raw data, never combined. [P, SfE; H; TN principle 10]

---

## 3. Choosing the form

The catalog's auto-chart (`show(signal)`) picks the form; explicit specs override.

| Question / data | Form (mark) | Avoid | Basis |
|---|---|---|---|
| Trend of one gauge/rate | `line+envelope` (mean + min/max band) | Mean only | TN, C R5 |
| Counter | rate with envelope; never raw cumulative value | Raw counter line | P, G (OTel) |
| Latency/size distribution over time | `heatmap` (x time, y value, colour count) | avg/max lines alone | P, C D1–D2 |
| "How many requests were fast enough?" (SLO-style) | fraction ≤ X over the window, merged by summing counts, with [lo, hi] bounds at bucket edges; draggable threshold readout | p99 line, averaged percentiles | P, H; SfE |
| Tail at a window ("how bad is the worst 0.1%?") | `ccdf` / `quantile_curve`, tail axis stretched | Percentiles cut at p95 | P2, C P4–P5 |
| Shape comparison now vs reference | `histogram` / `ecdf` with ≤4 windows | Overlaid density with hidden bin choice | P, C D12 |
| Spans >2 decades | log axis, labelled with variable+unit (not "log(x)") | Linear axis with bulk in a corner | P, C A7; TN §6.2 |
| Ratio vs baseline (p99 now / last week) | log axis, 1 centred | Linear ratio axis | P, C A8 |
| Sustained-shift detection in noise | smoothed line **over** raw/envelope, labelled + window | Smoothing alone | E, C R1–R4, R7 |
| Isolated-spike detection | raw / min-max / heatmap | Any smoothing | P, C R3 |
| Error ratio | Wilson band; normalized comparison → funnel plot (P2 phase) | Bare ratio line | TN |
| Success vs failed latency | **separate series** | One merged latency | P, C T7 |
| Saturation | most-constrained resource + its limit line; short-window p99 as early signal | Average utilization | P, C T8 |

**Never draw many series; aggregate and draw the analysis.** [TN] Readability collapses
past ~8 series, and no tested form keeps individual series readable beyond that ([C] M5,
M7, C4). A pile of lines is not evidence.

- **Group of series** (pods, instances, endpoints) → a **density cloud**, a canvas mark:
  x is time, y is value, and colour intensity is how many members sit at that value
  (heatmap-like, perceptually uniform colormap). The mean (or median) is drawn on top, and
  only the outliers are drawn as lines and labelled ("44 pods · 3 outliers · 2 silent").
  Spread is intensity, never upper/lower boundary lines. Outliers use robust rules
  (median + MAD / Tukey fences), never σ. [TN; P, SfE; P, C M5]
- **A few named series that are the question itself** (≤5, e.g. read vs write, prod vs
  canary) → lines in one plot. Shared space wins for *local* comparisons. [E, C M1]
- **Comparing aggregates across a few groups** (per region, per service) → small multiples
  of the aggregate views, **identical y-scale**. Split space wins for *dispersed*
  comparisons. [E, C M1, M6]
- Series heatmap (a row per member) only when member identity over time is the question.
- Horizon graphs: not offered. If ever added: ≤2 bands, opt-in, with a legend explaining
  layering. [E, C H2]

**One analysis, several related views.** An op can return several typed series aligned on
one time grid. The values come with companion series, for example `bucket_state`
(coverage and trust), counts, over-threshold fractions or estimator bounds. Latency, for
instance, comes with a secondary "fraction over threshold" series. The panel draws the
primary mark plus each companion's related marks: rug, count strip, a linked small panel,
a bound band. A companion is drawn only when it has something to say. Companions are
recomputed with the values every time ops are chained, and an op that cannot carry one
drops it *with* a caveat. [TN; spec 2026-10-02]

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
  represents an interval; a stepped path states that honestly. → Adopted: stepped by
  default (rule 12). [P, SfE; TN]

**Percentile definitions differ.** Hyndman–Fan list 9 types; software mostly uses type 7
(interpolated), probability theory type 1. Histogram-derived quantiles are a *range* (bucket
edges). → Provenance footer should state the quantile estimator ("bucket-edge bounds" /
"histogram_quantile linear interpolation"). [P, SfE]

**Deviation & outliers.** "68/95/99.7 within kσ" holds only for normal data — never for
latency. Use MAD or IQR, Tukey fences (k·IQR beyond quartiles); boxplots show median,
quartiles, outliers as points. Supports robust outlier rules for the group cloud (§3). Caveat: MAD/IQR
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

## 5a. Missing and untrusted data

"No info is itself info" (Hartmann). Full design:
`docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md`. Mockups were
agreed in the brainstorm: coverage rug with hover hints, plus textured heatmap columns.

**States** (`bucket_state`, a companion series computed per series and per bucket). Each
state means something different and must look different:

| state | meaning | rug | heatmap column |
|---|---|---|---|
| `ok` | observed ≈ expected samples | (no rug if every bucket is ok) | normal |
| `partial` | observed/expected < 0.9 | grey fill ∝ missing share | faded |
| `empty` | series alive, 0 samples | solid grey | dot texture |
| `absent` | series not alive yet (before its first sample in the window; trailing silence is `empty`, principle 9) | dotted line | — |
| `unknown` | fetch failed, outside retention, source can't tell | hatch | hatch texture |

Flags on top of the states: `reset` (↺ on the plot), `interval_change` (axis tick),
`stale_marker`, and `source_filled` (the backend invented the value).

**Rules:**
- **The rug appears only when something is wrong.** It is the visual form of the
  "missing / untrusted data" caveat (rule 13). On a clean panel, nothing is drawn.
- **Heatmaps: blank = measured, nothing happened.** No-data columns get a texture, so a
  measured zero never looks like "no observation". Measured-zero cells are not tinted, since
  that would ink almost every column.
- **Missing is rarely random.** The member that stops reporting is often the sick one
  (OOM-killed, saturated, partitioned), so a band over the survivors looks healthy *because*
  the sick ones dropped out.
  - A member that goes silent while still alive is listed and drawn **beside** the value
    outliers, labelled source undetermined (gone or sick: the data cannot tell; principle 8).
    Report it as "no samples since T", never as "left" (principle 9).
  - The group cloud normalizes intensity by **alive** members, so silent members visibly
    thin it.
  - Values over buckets where reporting < alive carry a caveat ("band over 41/44").
- **Window views** (histogram, ECDF, CCDF, threshold readout) have no time axis. They show a
  coverage badge ("covers 87% of window · 5m missing · 2 resets") that opens a mini rug.
  Fractions and counts carry the caveat when coverage < 100%.
- **Hover hints** on rug cells and textured columns show: span · state + reason · observed/
  expected samples at the resolution · last seen · for groups, `reporting/alive` and the
  silent members.
- **Missing data travels to Claude and to findings.** Summaries report coverage %, the
  longest gap, silent members, resets and unknown spans. A finding whose claim window
  overlaps a blocking caveat is rejected, or must narrow its scope.
- States are told apart by pattern (solid / dots / hatch / dotted), never by colour alone;
  greys must pass 3:1 against the background in both themes.

**Sources fabricate and hide data differently.** Prometheus-engine evaluation fills gaps
up to its 5m lookback. `rate`/`increase` extrapolate to window edges (Prometheus) or use
the sample before the window (VictoriaMetrics, so a possible fake spike after a gap).
Thanos/Mimir switch to downsampled tiers and can return partial responses. Rules:
- Read values only via `*_over_time` / `rollup` over the step window, never via instant
  evaluation.
- Whatever the source invents is flagged `source_filled`.
- A bucket we can't judge is `unknown`, never `ok`.
- Each adapter declares a `MissingDataSemantics` profile, pinned by fixtures. Per-backend
  evidence and open questions are in `docs/data-source-quirks.md` (spike `1h9.10`).

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
- Isolated single samples between gaps must be visible: with stepped paths, a lone
  bucket is a short horizontal segment; add a point marker if it is narrower than ~3 px.
  [X, C G3] → verify (`q4a`).
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

Tracked as beads (`bd show <id>`).

| # | gap | bead |
|---|---|---|
| 1 | Light-theme contrast: `#E69F00`, `#56B4E9` < 3:1 on white; per-theme darkened variants | `jfd` |
| 2 | Heatmap legend states colour mapping (`log1p` count / linear density); optional linear/rank blend | `4ok.13` |
| 3 | Accessible panels: ARIA name/description + view-as-table | `8ie` |
| 4 | Lone sample between gaps stays visible | `q4a` |
| 5 | Follow-up link: latency split by success vs failure | `2as.19` |
| 6 | Smoothing label convention | note on `4ok.9` |
| 7 | Exemplar seam on distribution datasets | `09h` |
| 8 | Indexed (ratio-to-baseline) view on log axis | `4ok.14` |
| 9 | Fraction-over-threshold op: merged, bucket-edge bounds, Wilson CI [H] | `4ok.12` |
| 10 | Threshold readout: draggable X → below / in-bucket / above [H] | `4ok.15` |
| 11 | Traffic count strip under percentile/heatmap plots [H] | `4ok.16` |
| 12 | Mergeability guard: pre-computed quantiles + non-mergeable statistics [H, SfE] | `2as.20` |
| 13 | Classic-histogram `le`-set mismatch guard [SfE] | `4ok.17` |
| 14 | Quantile estimator in provenance footer [SfE] | `4ok.18` |
| 15 | Spectrum Nyquist cut at 2×step [SfE] | note on `4ok.7` |
| 16 | Multi-threshold banded counts + survival-curve overlays [SfE] | note on `4ok.11` |
| 17 | Missing data: `bucket_state`, rug, heatmap textures, stepped paths, localized caveats, failed-chunk `unknown` spans | spec 2026-10-02; phases 1–4 done (plan 2026-10-02-missing-data-bucket-state) |
| 18 | Per-backend missing-data semantics (lookback, staleness, rate edges, tiers, partial responses) | `1h9.10` |
| 19 | Group density cloud mark with silent members | spec 2026-10-02 §7.1; visual design to brainstorm |

## 11. Open questions (no evidence either way)

- Dark-mode perception; HiDPI pixel mapping of the sizing studies.
- Effectiveness of annotation/deploy markers; live-chart awareness (Ragan et al. 2020 unread).
- Best nonlinear heatmap colour mapping for exponential/native-histogram buckets.
- `partial` threshold (0.9 of expected samples) under scrape jitter and OTel push intervals.
- Group-cloud binning, colormap and outlier rule.
