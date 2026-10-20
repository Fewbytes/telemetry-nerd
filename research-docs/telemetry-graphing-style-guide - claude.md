# Telemetry Graphing Style Guide (v2)

Scope: how to *display* telemetry in a computer UI. Not storage, not aggregation.
Built from sources actually fetched and read (listed at the end). Where a rule is my own extrapolation, it is tagged and should be treated as a hypothesis. v2 closes most gaps listed in v1; remaining gaps are in section 14.

## How to read the tags

| Tag | Meaning |
|-----|---------|
| **[E]** | Result from a controlled experiment in a source I read. Conditions in section 10 and 13. |
| **[P]** | Practitioner argument, specification, or design opinion from a source I read in full. Reasoned, not experimentally tested. |
| **[P-2nd]** | Practitioner claim I only have secondhand (someone else's notes, a search excerpt, a blog/forum). Weaker. |
| **[X]** | My inference or extrapolation. Not stated in any source. Verify before relying on it. |

Source IDs (S1–S15) are defined at the bottom.

---

## 1. Core rules (apply everywhere)

1. **Prefer position on a common scale over every other encoding for quantities.** Position beat length, and both beat angle and area, in a crowdsourced replication of Cleveland & McGill. [E, S3]
2. **Linear bars start at zero.** Bar length must be proportional to the value. [P, S7, S6]
3. **Filled areas under a line are valid only if the y-axis starts at zero.** [P, S7]
4. **Don't make the viewer do arithmetic.** If the question is "how far off target," plot the deviation directly. [P, S6]
5. **Show context with every key measure:** target, history, or qualitative state. [P, S6]
6. **Use the same chart type for the same kind of data.** [P, S6]
7. **Color must carry meaning; same color, same meaning across the display.** Subdued by default, saturated only to highlight. [P, S6, S7]
8. **Never encode information by color alone.** Add position, shape, label, or order. [P, S7]
9. **Cut decoration and excess precision.** [P, S6]
10. **Don't show the mean alone for latency-like data.** Show the distribution or at least the tail. [P, S14, S4]

---

## 2. Choosing the display

| Task | Use | Avoid | Basis |
|------|-----|-------|-------|
| Trend of one series | Line (drop dots when dense) | Dots-only when many points | P, S7 |
| Compare a few series at one moment (local) | Shared-space line chart | Small multiples spaced far apart | E, S2 |
| Compare many series / dispersed points | Small multiples, **identical y-scale** | Overlaid lines as series count grows | E, S2 |
| Vertical space is the binding constraint | Horizon graph, 2 bands, small heights | 3+ bands | E, S1 |
| Latency / size / utilization **distribution over time** | Heat map (x=time, y=value, color=count) | Avg/max lines alone | P, S4 |
| Latency tail at a point in time (e.g., a load test, an incident window) | Percentile-distribution chart (value vs percentile, tail stretched) | Percentile lines cut off at p95 | P-2nd, S11 |
| Value spans orders of magnitude, skewed | Log value axis (state base/units), ECDF or heat map | Linear axis where the bulk collapses into one corner | P, S10; application X |
| Noisy series, task is spotting a *sustained* shift | Smoothed line with raw/min-max underneath | Raw-only at high point counts | E, S15 |
| Task is spotting isolated spikes | Raw or min/max-preserving rendering, heat map | Moving-average smoothing alone | P, S15, S4 |
| One measure vs target, many metrics in small space | Bullet graph | Radial gauges | P, S5, S6 |
| Ranking / comparing amounts | Horizontal bar or dot plot | Pie when numbers must be read | P, S6; E, S3 |
| Two related metrics over time | Two line graphs stacked, shared time axis | Connected scatter unless direction/time marked | P, S7 |
| Individual-series comparison | Lines or small multiples | **Stacked areas** | E (secondhand via S1/S2) |

---

## 3. Time series line charts

- **T1.** Lines connect ordered points; they're a guide to the eye. Omit markers when dense. [P, S7]
- **T2.** Label lines directly at their ends instead of using a legend. [P, S7]
- **T3.** If a legend is needed, order entries to match the vertical order of lines at the right edge. [P, S7]
- **T4.** Banking to ~45° average segment slope improves slope perception. [E, **secondhand** via S1; Cleveland not read] When axes have different units, stretching one relative to the other is valid; pick an aspect ratio at which important differences in position are noticeable. [P, S10 ch.3]
- **T5.** Animation between states gave lower accuracy than static small multiples in analytic tasks. [E, **secondhand** via S1/S2]
- **T6.** Connected scatter needs explicit direction and time cues. [P, S7]
- **T7.** Separate series for successful vs failed requests. A fast error flatters the latency number; a slow error is worse than a fast one. [P, S14]
- **T8.** For saturation, show the most-constrained resource and draw the utilization target. Many systems degrade before 100% utilization. A p99 over a short window (the source says ~1 minute) is an early saturation signal. [P, S14]

### Missing data (weak evidence)

No experimental or authoritative source found. The following is from a blog post and tool forum threads.
- **G1.** A line drawn across a gap implies continuity that isn't there. Prefer leaving the gap visible; optionally fade or highlight the missing span, or annotate it. [P-2nd, blog]
- **G2.** Common tools expose gap / zero-fill / connect modes. Zero-fill asserts a measurement of 0; connect asserts continuity. [P-2nd, forum threads; consequence is X]
- **G3.** A single sample between two gaps has no line segment and can disappear; show point markers for isolated samples. [X]
- **G4.** Tell "no data" apart from "zero" visually. [X]

### Resolution, smoothing, refresh (S15, Stanford, VLDB 2017)

Task studied: locate the region containing a described *sustained* anomaly in a plot; 700 crowd workers, 5 datasets with ground truth, plots 800 px wide. Not a spike-detection study.

- **R1.** Smoothing improved accuracy over raw data by 21.3% on average and speed by 23.9%; up to 38.4% accuracy on one dataset. Compared with other methods (downsampling, line simplification, etc.) the average gain was ~35%. [E, S15]
- **R2.** Smoother is better but only up to a point. In a sensitivity study, plots 4–8× rougher than the optimum scored ~56–62% accuracy vs ~79–80% for the optimum and half-as-rough. Oversmoothing hides the trend entirely. [E, S15]
- **R3.** Smoothing averages away isolated spikes. S15's method refuses to smooth when a few extreme outliers dominate. Use smoothing for sustained deviations, not spike detection. [P/E, S15]
- **R4.** S15 suggests overlaying the smoothed series on a shape-preserving rendering of the raw series (they name M4 downsampling, which I did not read). [P-2nd, S15]
- **R5.** Don't plot more points than pixels. Aggregate to the point-to-pixel ratio. Their anecdote: on a phone, ~4000 CPU readings crowded into a thick band that made a spike look like stability. [P, S15 anecdote]
- **R6.** People perceive changes on the order of 60 events/s, so re-rendering on every incoming point is pointless; refresh at human-perceptible intervals (their example: ~1 Hz). [P, S15, citing another study]
- **R7.** Smoothing is a deliberate distortion of the data. Label it as smoothed and give the window. [X]

---

## 4. Many series: shared space vs split space vs horizon

Findings from S2 (16 participants, 3,456 trials, synthetic positive-valued data, static, quick-overview tasks, 2/4/8 series):

- **M1.** Shared-space faster for **local** tasks; split-space (small multiples, horizon) faster for **dispersed** tasks. [E, S2]
- **M2.** Series count strongly hurt accuracy and time. Space per chart hurt accuracy, not time. [E, S2]
- **M3.** Plain lines and small multiples were most robust across tasks. Horizon was slowest on the simple max task. [E, S2]
- **M4.** Informal 4-person follow-up up to 16 series: split-space scaled better. **Not powered.** [E-weak, S2]
- **M5.** Past ~8 series, no tested form keeps individual series readable. The authors' answer is aggregation or query-based filtering. [P, S2]
- **M6.** Small multiples must share the y-scale. [P, S2]
- **M7.** Eight+ colors on thin lines are hard to tell apart. [E-ish, S2; S7]

Horizon graphs (S1, S2):
- **H1.** Bands layered, negatives mirrored; height reduction ×(2 × bands) for signed data. [S1]
- **H2.** **At most 2 bands.** 4+ bands raised error and time; 3 bands slower than 2 with similar accuracy. [E, S1]
- **H3.** Layering costs ~2 s per comparison. Mirroring alone does not. [E, S1]
- **H4.** Needs a per-series baseline for the bands to be used; differing baselines make cross-series comparison hard. [P, S2]
- **H5.** Latency/CPU data is usually positive-only, so mirroring gives nothing; you get layering only and must pick a baseline. [X]
- **H6.** Viewers must be taught to read it. [P, S1]
- Few's original horizon article (S1's reference 7) could not be retrieved; S1 and S2 are the primary evidence.

---

## 5. Distributions, tails, percentiles, skew

### Heat maps (S4)

- **D1.** x=time, y=value range, color=event count in that time×value cell. [P, S4]
- **D2.** Averages are dominated by the fast majority and hide multi-modal structure (his DRAM / flash / disk bands). A max line can't tell one outlier from many. [P, S4]
- **D3.** Cells must be large enough to hold several events or the map degenerates into a scatter plot. [P, S4]
- **D4.** **Linear color scaling washes out rare events** (<1% of traffic may be nearly invisible). A false-color/nonlinear mapping reveals them but makes shade unreliable for comparing counts between cells. Choose deliberately and say which. [P, S4]
- **D5.** Middle path: perceptually uniform sequential palette plus a nonlinear value-to-color mapping, rather than rainbow false color. Grafana's heatmap exposes an exponent color scale (option list only). [X; S9]
- **D6.** Auto-scaling y to include outliers compresses the bulk. Offer trimming of the top slice (he cites 0.1%) and zoom. [P, S4] Disclose when trimming is on. [X]
- **D7.** Keep finer data available for re-rendering at other ranges; state the resolution limit shown. [P, S4]
- **D8.** A side panel with per-row averages helped. [P, S4]
- **D9.** Show read and write latency separately. [P, S4]
- **D10.** Utilization heat map: y=utilization %, color=number of components. Tight dark band = balanced; diffuse cloud = not; one device pinned at 100% is a faint top line a max can't distinguish from many. [P, S4]
- **D11.** Measuring CPU at one-minute averages hides spikes that drive tail latency. Counting each second into utilization buckets and aggregating per minute preserves brief hotspots. [P, S14]; consistent with D10.

### Tails and percentiles

- **P1.** If a service averages 100 ms and 1% of requests take 5 s, the rest are only ~2× faster than the average. Without the distribution, "most requests are near the mean" is a guess. [P, S14]
- **P2.** Histogram buckets with roughly exponential boundaries (their example: ×3 steps: 0–10, 10–30, 30–100, 100–300 ms) make the distribution visible. [P, S14]
- **P3.** If a page makes many calls, a high backend percentile becomes the frontend median. [P, S14]
- **P4.** **A percentile chart that stops at p95 gives a false impression of the common case.** Thought experiment, assuming independent requests and ~200 requests per session: only ~0.0035% of sessions have all 200 requests under their p95; to cover 99% of sessions you need roughly the 99.995th percentile per request. [P-2nd, S11 notes of Tene's talk; arithmetic checked, independence assumption is X]
- **P5.** Percentile-distribution chart: value on y, percentile on x, with the tail stretched so each extra nine gets room. HdrHistogram output carries a 1/(1−percentile) column and "exponentially increasing fidelity"; its plotter has a "how many nines" option. I did not see a rendered plot, so the exact axis scaling is inferred. [P-2nd, S11; scaling X]
- **P6.** Plot the max. It is a real observation, not a statistic. [P-2nd, S11]
- **P7.** Requirement lines can be drawn directly on a percentile-distribution chart. [P-2nd, S11]
- **P8.** Don't compute or display an "overall" percentile as the average of per-interval percentiles. Per-interval percentile summaries can't be averaged into a run percentile. [P-2nd, S11 (Tene, excerpt only)]. This is an aggregation point but affects what a chart's caption can honestly say.
- **P9.** Tension: S4/S11 say show outliers and the max; S15 says smooth to expose sustained shifts. Resolve by task (§2), and when both matter, overlay (R4) or use a heat map. [X]

### Skewed and heavy-tailed values (S10 ch.8)

- **D12.** Histograms and density plots depend on user-chosen bin width / bandwidth and are an interpretation of the data. ECDFs and q-q plots need no such parameter and show all the data. They are less intuitive for readers. [P, S10]
- **D13.** Raw heavy-tailed values produce near-useless density and ECDF plots. If log-normal, log-transforming gives a bell curve and a sigmoid ECDF. A descending ECDF on log–log axes shows a power-law tail as a straight line. Check the shape with a q-q plot rather than assuming. [P, S10]
- **D14.** Latency being heavy-tailed or log-normal is not claimed by S10. For heatmap bucket boundaries, which are also a parameter choice, consider log-spaced rows as the SRE chapter suggests (P2). [X]

---

## 6. Axes, scales, gridlines

- **A1.** Linear bars start at 0 (amounts). Bars on a log scale encode **ratios** and start at 1. [P, S10]
- **A2.** To show an *amount* on a log axis, use a dot, label next to the dot. [P, S10]
- **A3.** A line chart on a truncated axis is questionable for judging magnitude even without a fill. Make the baseline obvious or plot deviation. [P, S10]
- **A4.** Gridlines ≥ ~8 px apart; some gridlines improved accuracy; 10 vs 20 units and 50 vs 100 units not significantly different. [E, S3]
- **A5.** Gridline alpha ~0.2 is a safe default on light-to-mid gray backgrounds. **Dark backgrounds untested.** [E, S3; X]
- **A6.** Accuracy plateaued by ~80 px height on a 0–100 scale; 40 px significantly worse. [E, S3] (not directly comparable with S1, see §12)
- **A7.** Log scale: label the axis with the variable's name and units, not "log(x)". If plotting log-transformed values, state the base. Zero can't appear on a log axis. A square-root scale admits zero but a unit step has no constant meaning and tick placement is awkward. [P, S10 ch.3]
- **A8.** Ratios (e.g., p99 vs last week) belong on a log scale, with 1 as the midpoint. On a linear scale, values above 1 are exaggerated and below 1 obscured. [P, S10 ch.3; applying it to telemetry ratios is X]
- **A9.** Larger axis labels help. [P, S10 TOC only, chapter not read]. Treat as unverified.

---

## 7. Color and contrast

- **C1.** **No rainbow/jet for continuous values.** Non-monotonic lightness; emphasizes arbitrary ranges. Use a perceptually uniform, lightness-ordered sequential map. [P, S7; S8 abstract]
- **C2.** Sequential maps are generally CVD-safe. **Red–green diverging is not**; pick pairs differing in blue content (pink/yellow-green style). [P, S7]
- **C3.** CVD prevalence: S7 says ~8% of males, ~0.5% of females; S6 (2006) says 10% and 1%. Sources disagree; design for it either way.
- **C4.** Qualitative palettes: 3–5 categories good; 8–10+ too many. Beyond ~8 use direct labels. [P, S7]
- **C5.** 8-color CVD-safe palette (Okabe–Ito): `#E69F00`, `#56B4E9`, `#009E73`, `#F0E442`, `#0072B2`, `#D55E00`, `#CC79A7`, `#000000`. [P, S7]
- **C6.** Colors are harder to tell apart on thin lines and small marks, worse under CVD. **Test with a CVD simulator.** [P, S7]
- **C7.** Avoid large filled areas of saturated color. Light fill, darker same-hue line/text. [P, S7]
- **C8.** Same variable on position and color: integrate the color bar into the axis. [P, S7]
- **C9.** Status ranges: intensity steps of **one hue**, not distinct hues (§8). [P, S5, S6]
- **C10.** Horizon graphs conventionally use blue/red. Not CVD-verified in sources. [S1, S2; X]
- **C11.** **Contrast standard (WCAG 2.x, SC 1.4.11):** graphical objects needed to understand content need at least 3:1 contrast against adjacent colors. It doesn't apply where the same information is available in another form (e.g., direct value labels). Thin lines can be anti-aliased to a fainter color than the nominal CSS, so a nominal pass can fail in practice. [P, S12; W3C excerpt]
- **C12.** Tension: S3's recommended gridline alpha (~0.2) sits well below 3:1. Whether gridlines count as "needed to understand" is an interpretation. One university checklist treats gradation lines as in scope; I saw no W3C text addressing gridlines. Unresolved. [X]
- **C13.** **Dark mode perception remains untested** in every source. Use C11 as a floor, not as evidence that a palette works. [X]

---

## 8. Dashboards, thresholds, signals (S5, S6, S14)

Business-BI era guidance (S5, S6) plus SRE guidance (S14). Perceptual principles transfer; chart lists reflect their era. [X]

- **B1.** Single screen, glanceable; fragmenting forces memory use. [P, S6]
- **B2.** Support: big picture → items needing attention → drill-down. [P, S6]
- **B3.** If everything is prominent, nothing is. [P, S6]
- **B4.** Round to what's actionable. [P, S6]
- **B5.** Pick the best display for the data even if it repeats. [P, S6]
- **B6.** Place things meant to be compared next to each other. [P, S6]
- **B7.** A dashboard is prebuilt to answer basic questions and normally includes some form of the four golden signals (latency, traffic, errors, saturation). It may also show recent pushes (deploys) and similar team info. [P, S14]
- **B8.** Debugging use: "latency just shot up; what else changed around then?" implies aligned time axes and visible change events. Deploy markers on charts are my inference from the SRE text. [P, S14; markers X]
- **B9.** SRE teams avoid depending on someone staring at a screen. Subcritical issues belong on a dashboard rather than in email alerts. Signals neither on a dashboard nor in an alert are candidates for removal. [P, S14]
- **B10.** The SRE chapter defines error-by-policy (e.g., any request over a committed time counts as an error). A threshold line at that committed value is a natural overlay. [P, S14; overlay X]

**Bullet graph (S5):**

| Part | Rule |
|------|------|
| Label | Left of horizontal, above vertical. |
| Scale | Single linear axis, from zero (or negative). May start above zero, but then the featured measure is a dot/X, not a bar. |
| Featured measure | Dark bar, ~⅓ container thickness, no border. |
| Comparative measure(s) | 1–2 short perpendicular lines behind the bar; second lighter (75% black). |
| Qualitative ranges | **Max 5, ideally 3.** Single hue, dark=poor, light=good. 2: 35/10% black. 3: 40/25/10. 4: 50/35/20/10. 5: 50/35/20/10/3. No borders. |
| Lower-is-better | Reverse range order (and optionally scale/bar direction). |
| Optional | Projection segment to period end. |

---

## 9. Interaction (S13, Shneiderman 1996)

1996 paper; the principles are still the standard citations, the specifics are dated.

- **I1.** Mantra: overview first, zoom and filter, then details on demand. [P, S13]
- **I2.** Seven tasks: overview, zoom, filter, details-on-demand, relate, history (undo/replay/refine), extract (save/export a subset or the settings). Few prototypes supported history or extract. [P, S13]
- **I3.** Overview + detail: overview with a movable field-of-view box, zoom factors of ~3–30 per level; add intermediate levels for larger factors. Fisheye zoom factors were limited to ~5. [P, S13]
- **I4.** Smooth zooming helps preserve position and context. [P, S13]
- **I5.** Dynamic filter controls should update the display in under ~100 ms, even with tens of thousands of items. [P, S13]
- **I6.** For telemetry: a time-range selection that carries across panels, shareable/restorable view state, and an export of the current view map onto I2 (relate, history, extract). [X]

---

## 10. Sizing numbers (with conditions)

| Item | Value | Basis and conditions |
|------|-------|----------------------|
| Line / 1-band mirrored chart, best speed with no accuracy loss | ~24 px | E, S1. 14.1″ 1024×768 LCD (~6.8 mm). Compare one point across two charts, ±5 tolerance on ±100 range. Error flat 24–48 px. |
| 2-band horizon | Use below 24 px; optima 12 and 6 px; keep ≥ 6 px | E, S1. Same conditions. |
| Error vs height below that | ~3.5–4 value-units per halving of virtual resolution (±100 range) | E, S1. |
| Floor | At 2 px viewers fell back to reading color | E, S1. |
| Web line/bar chart accuracy plateau | ≥ ~80 px; 40 px worse | E, S3. 0–100 scale, 12 labeled points, crowdsourced. |
| Gridline spacing | ≥ ~8 px | E, S3. |
| Per-series height in small multiples | down to 6 px (up to 16 series, informal) | E-weak, S2. |
| Series per display | 2/4/8 tested; plan to filter/aggregate past ~8 | E + P, S2. |
| Points per plot | ≤ pixel width | P, S15. |
| Refresh | ~1 Hz example; no need to exceed human-perceptible rates | P, S15. |

**Pixel values come from low-DPI 2009–2010 displays.** Mapping to CSS pixels on high-DPI screens was left to future work in S1. [X]

---

## 11. Pre-flight checklist

1. Viewer's task: local compare, dispersed compare, sustained shift, isolated spike, distribution, tail, vs target? Pick from §2.
2. Quantities by position on a common scale; bars from 0; fills only with zero baseline; log axes labeled with variable and units.
3. ≤ ~8 series per chart; small multiples share y-scale.
4. Latency-like data: not mean alone. Tail visible (heat map, percentile-distribution, or at least p99 and max). No percentile cut-off at p95 as "typical."
5. Success vs failure latency separated; saturation shows the constrained resource with its target.
6. Heat map: cells large enough; color mapping stated; outlier trim disclosed.
7. Smoothing used only for sustained-shift tasks, labeled, with raw or min/max available.
8. Points ≤ pixels; aggregation preserves spikes if spikes matter.
9. Gaps visible; zero ≠ no data.
10. Color: perceptually uniform sequential, no rainbow, ≤ 5 categorical colors (direct labels past ~8), non-color cue present, CVD-simulated, 3:1 against background for essential marks.
11. Direct labels over legend; legend in visual order if present.
12. Context present; variance shown directly; precision trimmed; one screen; most important most prominent; nothing decorative.
13. Interaction: overview first, zoom/filter, details on demand, shared time range.

---

## 12. Where sources disagree or don't line up

- **Chart height:** S1 (24 px) vs S3 (~80 px). Different task, scale, chart type, gridlines, pool. Treat each as a tested range under its own conditions.
- **Outliers:** S4/S11 push for seeing the max and rare events; S15 smooths them out to expose sustained shifts. Different tasks (§2, P9).
- **CVD prevalence:** S6 vs S7 (C3).
- **Gridline alpha vs contrast:** S3 vs WCAG 3:1 (C12).

## 13. Limits of the evidence

- S1, S2 measured value comparison and quick overview, not rate-of-change or sustained monitoring. S2: 16 participants, synthetic positive-only data, static. Its high-series follow-up had 4 participants.
- S3 is crowdsourced; display size, gamma, viewing distance uncontrolled; gridline study used light-to-mid gray backgrounds only.
- S4 is one practitioner's argument with real traces; no comparison against alternatives with human subjects.
- S5, S6 are expert opinion about business dashboards.
- S11 is third-party notes of a talk I did not watch, plus README and excerpt text.
- S15's user studies measure locating a described sustained anomaly in one of five regions, on crowd workers, at 800 px.
- Taken secondhand: Cleveland–McGill rankings (S1, S3), banking to 45° (S1), animation vs small multiples (S1, S2), M4 downsampling (S15).

## 14. Remaining gaps

- Counters vs gauges vs rates (scoped out earlier as aggregation).
- Evidence on **live-updating charts** beyond refresh rate (S15). A relevant paper on awareness during streaming display (Ragan et al., AVI 2020) surfaced but I did not read it.
- Missing-data handling has only blog/forum sources (§3).
- Annotation/deploy-marker effectiveness: no study read.
- Dark-mode perception, high-DPI pixel mapping: untested in everything read.
- Exponential/native histogram rendering; bucket-boundary artifacts in heat maps.
- Few's "Time on the Horizon" (not retrievable), Munzner, Healy, ColorBrewer/viridis docs, Datawrapper, FT Visual Vocabulary, Honeycomb, flame graph articles, Wilke ch.4/7/18/21/23, M4 paper, Cleveland & McGill: not read.
- Grafana heatmap docs: option list only; no design guidance.

---

## Sources

**Read in full:**
- **S1** Heer, Kong, Agrawala. *Sizing the Horizon.* CHI 2009.
- **S2** Javed, McDonnel, Elmqvist. *Graphical Perception of Multiple Time Series.* IEEE TVCG 2010.
- **S3** Heer, Bostock. *Crowdsourcing Graphical Perception.* CHI 2010.
- **S4** Gregg. *Visualizing System Latency.* CACM, July 2010.
- **S5** Few. *Bullet Graph Design Specification.* Rev. 2013.
- **S6** Few. *Common Pitfalls in Dashboard Design.* 2006 whitepaper.
- **S7** Wilke. *Fundamentals of Data Visualization*, ch.13, 17, 19, 20.
- **S10** Wilke, same book, ch.3 (coordinate systems and axes) and ch.8 (ECDF, skewed distributions, q-q plots).
- **S13** Shneiderman. *The Eyes Have It.* 1996.
- **S14** Ewaschuk. *Monitoring Distributed Systems*, Google SRE book ch.6.
- **S15** Rong, Bailis. *ASAP: Prioritizing Attention via Time Series Smoothing.* VLDB 2017.

**Read in part (excerpts):**
- **S8** Crameri, Shephard, Heron. *The misuse of colour in science communication.* Nat. Commun. 2020. Abstract/excerpts.
- **S9** Grafana heatmap docs. Option list.
- **S11** Gil Tene, "How NOT to Measure Latency": third-party notes (uucidl gist) read in full; HdrHistogram.NET README, hdr-plot README, and an excerpt of Tene's "average of percentiles" text read as search excerpts only. The talk itself not viewed.
- **S12** W3C *Understanding SC 1.4.11 Non-text Contrast*: excerpt plus secondary summaries.
- Gregg heat map / utilization pages and LISA 2010 slides: excerpts.
- Missing-data guidance: one blog post and forum threads (weak).
