# UI layer contract and optional compute server: spec

Card `telemetry-nerd-h4es`, from the king's brainstorm of 2026-11-24 (approved 2026-11-25).
Spec only; implementation cards are filed from it, none before it lands. Rules from
`docs/principles.md` and `docs/telemetry-graphing-guide.md` win over this document on any
conflict. Builds on the landed epic `telemetry-nerd-rnkq` (`plot_data`, `plot_csv`, never
pie; see `design-docs/visualization-service-for-claude.md`).

## 1. Why split

Simple mode (`docs/modes.md`) only removes chrome from one product. The king wants two
products with a hard seam between them:

- **UI layer**: a toolkit that lets a coding agent *display* data honestly, whatever its
  origin: Grafana, Elasticsearch, a CSV, an MCP tool result, or numbers the agent computed.
  It draws; it does not decide what the data means.
- **Compute server** (optional): models and other heavy computation. Advanced panel
  features come from it, and only when the caller opts in.

The UI layer is complete without the compute server. An agent that never connects one gets
every chart in section 5 and every metric set in section 6.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| Caller | The coding agent (or script) using the toolkit |
| Adapter | Turns one origin into the UI layer's input form (section 3.2) |
| Panel | One drawn answer to one question |
| Metric set | Several related series drawn and read together (section 6) |
| Agent-only mode | The UI layer with no compute server attached |
| Opt-in | An explicit per-call or per-session declaration by the caller that it wants a compute feature (section 4.3) |

## 3. UI layer contract

### 3.1 Inputs

The one input the UI layer understands is **caller-supplied data in the typed forms of
`visualization-service-for-claude.md` 2.2** (time series, array, labelled columns, matrix),
parsed once at the boundary into a validated table. Every call carries:

- `question`: what the panel answers (principle 5). Required.
- `kind`: a chart kind from section 5, or omitted where the shape fixes it.
- `unit?`, `labels?`, and for metric sets the `set` form of section 6.

The UI layer never receives a query string it must execute against a source on its own
authority, and never reads a file or URL named by the caller. Fetching is the adapter's job.

### 3.2 Adapters

An adapter converts an origin into the input form and attaches **provenance** (which
origin, which expression, which window). The UI layer draws what the adapter returns and
shows the provenance on the panel.

| Origin | Adapter | Status |
|---|---|---|
| Grafana, Prometheus/PromQL, Elasticsearch | the existing `sources` registry | exists |
| CSV text | `plot_csv` | exists |
| Numbers held by the agent | `plot_data` | exists |
| MCP tool result (any server) | the caller extracts the numbers and calls `plot_data`; no MCP-specific adapter in the contract | caller side |
| Any other origin | the caller converts to the input form | caller side |

"Any data source" is satisfied by the input form, not by an ever-growing adapter list: if
the caller can produce the typed table, it can draw it.

An adapter that cannot say where data came from marks it `supplied by caller`; it never
invents a source.

### 3.3 Outputs

- A **panel** in the shared workspace, addressable by id, with `url`.
- A **tool result** that states, in text the agent will read: the question, what was drawn,
  `n` per series, missing count per series, limits applied, provenance, and any refusal.
  Bulk data never returns to the agent's context (principle 6).
- PNG/PDF export of the panel is the same truth as the screen.

### 3.4 Honesty rules

These bind every chart kind and every adapter. None has an override.

1. **`n` and missing are stated** on the panel and in the tool result, per series. Missing
   values are gaps, drops that are counted, or "no value"; never zero (principle 11).
2. **Never pie.** Pie, donut, radial, sunburst, 3-D and stacked area are refused with
   `refused_form`. No flag changes this.
3. **Refuse over limits; never truncate or downsample silently.** Limits are those of
   `plot_table.py` (lines per plot, points per series, values per sample, bars, matrix side,
   CSV bytes, label length). A refusal opens with its stable code (`over_limit`,
   `not_finite`, `ts_not_increasing`, `refused_form`, ...) and names the series or column and
   the index that failed.
4. **The scale is stated.** Zero baseline on bars; a truncated or log axis is badged
   (principle 3).
5. **Supplied data is not evidence about a system.** Panels from `plot_data` and `plot_csv`
   are labelled "supplied by caller"; a panel from a source carries that source's scope and
   caveats (principle 2).
6. **No model output without its label.** Anything the compute server produced is marked as
   a model result with its method and parameters (principle 16). In agent-only mode there
   is nothing to label, because there are no model results.
7. **Same truth in every rendering.** Simple and nerd modes differ in chrome only
   (principle 18).

## 4. Optional compute server

### 4.1 What it is

A separate process that offers computation the UI layer does not do itself: statistical
models, forecasting, anomaly or change-point detection, spectral analysis, and the panel
features built on them. The UI layer calls it over a defined request/response interface and
draws the result.

The compute server holds no UI. The UI layer holds no model.

### 4.2 Excluded from this contract

Advanced models are **not** part of the UI-layer contract, and this spec does not define
them. Statistical process control (SPC) in particular is excluded: neither the UI layer nor
the agent-only skills require it, and no UI-layer feature may depend on it. Whether and how
the compute server offers SPC or any other model is for its own spec.

### 4.3 Opt-in

Advanced panel features are off by default. They are used only when the caller opts in:

- The caller names the feature in the call (for example a `compute` field naming the
  model) or enables a compute profile for its session.
- Having a compute server attached does not opt in. Presence is capability, not consent.
- A call that does not opt in is drawn by the UI layer alone, with no model output on it.
- A call that opts in while no compute server is reachable fails with
  `compute_unavailable`, naming the feature and saying that dropping the `compute` field
  draws the plain chart. It never falls back silently to a plain chart.

### 4.4 Agent-only mode

With no compute server:

- Every chart kind in section 5 and every metric set in section 6 works.
- Panels carry no model results, forecasts, bands from models, or verdicts.
- Analysis is whatever the agent does itself, for example with PromQL or SQL it runs
  against its own source, then draws with the UI layer. Gang (fleet) analysis is the
  worked example: it needs only PromQL (per-instance series, grouping, ratios to the
  fleet median) and ships as an **agent-side skill** (section 7), not as a compute-server
  feature and not as part of this contract.
- Nothing is degraded or hidden to nudge the caller toward the server.

## 5. Chart kinds

Simple and honest, as good as they can be made. Kinds that exist today are marked
**exists**; the rest are required by this spec. Every kind obeys section 3.4.

| Kind | Answers | Honesty rule specific to the kind | Status |
|---|---|---|---|
| `line` | How does it move over time? | Gaps where values are missing; at most 5 lines per plot; no smoothing, no interpolation across gaps | exists |
| `bar` | How large is each category? | Zero baseline, descending sort, never truncated, never log; missing is "no value" | exists |
| `bar_grouped` | Several series per category | One shared scale across groups; 2 to 4 series | exists |
| `histogram` | What shape is the sample? | Bin edges stated; counts, not density, unless labelled; samples compared on shared edges; dropped values counted | exists |
| `heatmap` | A matrix of values; or a distribution over time (below) | Colour scale stated and sequential; missing cells are blank, never the lowest colour; no cell smoothing | exists (matrix); histogram-derived form required |
| `ecdf` | Fraction of the sample at or below x | Step function through the actual sample, no interpolation; y runs 0 to 1; `n` stated | exists |
| `ccdf` | How heavy is the tail? | Fraction above x; log axes allowed only when badged; `n` stated, and the smallest resolvable tail fraction (1/n) marked so a flat tail beyond it is not read as zero | exists |
| `quantile_curve` | Value at each quantile | Quantiles computed from the supplied sample only; `n` stated | exists |
| `qq` | Do two samples differ, and where? | Exactly two samples; reference diagonal drawn; both `n` stated | exists |

### 5.1 Histogram-derived heatmap

A series of histograms over time (Prometheus classic or native histogram buckets, or an
Elasticsearch histogram aggregation) is drawn as a **heatmap**: time on x, value bucket on
y, cell colour = count (or per-interval rate of count). Rules:

- Bucket boundaries are the source's own; the UI layer does not rebin silently. If it
  merges buckets for legibility it says so and states the merge (principle 10: only
  mergeable statistics are aggregated; percentiles never are).
- Cumulative buckets are differenced to per-bucket counts before colouring; counter resets
  are gaps, not negative counts.
- The colour scale is shared across the whole panel and its legend gives the unit (count
  or rate).
- A percentile overlay is allowed only when computed from the same buckets, and is badged
  with the bucket resolution that bounds its error.

### 5.2 CDF family

`ecdf`, `ccdf` and `quantile_curve` are computed by the UI layer from the supplied sample
or histogram buckets. From buckets, the curve is drawn at bucket edges only (the only
points the data supports) and the within-bucket interpolation is not drawn as if it were
data.

## 6. Metric sets

A **metric set** is several related series looked at together: for example the request
rate, error rate and p99 latency of one service; or CPU, memory and GC pause of one
instance. The question belongs to the set, not to each series.

Contract:

- Input: `set: [{name, role?, series | data, unit?}]` plus one `question`. All members are
  adapter outputs or caller data in the section 3.1 forms.
- Output: one workspace entry of aligned panels, one per member, sharing the **same time
  axis, window and crosshair**. Members keep their own y scales and units; units are never
  merged or silently converted.
- Alignment is stated: if members have different step or coverage, the panel shows each
  member's step and coverage and does not resample one onto another without labelling it.
- Per-member `n` and missing are stated (section 3.4.1); one member's gap does not blank
  the others.
- Limits: at most 6 members per set; each member obeys its kind's limits. Over the limit,
  refuse with `over_limit`; the caller splits into two sets.
- A set draws; it does not correlate. Any statement of relationship between members
  ("these move together") is the agent's claim, not the panel's. A correlation or causal
  overlay is a compute-server feature and requires opt-in (section 4.3).
- Agent-only mode supports metric sets in full.

## 7. Agent-side skills, tools and catalog (not contract)

The contract (sections 3 to 6) is the stable seam. Reusable agent-side material built on
it is **not** part of the contract and may change without a contract revision. It is
specified and tracked by the companion skills/catalog card.

| Belongs to the contract | Belongs to agent-side skills, tools, catalog |
|---|---|
| Input forms, `question`, `kind`, `set` | Which kind to pick for which question (e.g. `plot-mode` skill) |
| Honesty rules, refusals and their codes | Wording that teaches an agent to avoid refusals |
| Chart kinds and their per-kind rules | Recipes: PromQL for rate/error/latency sets |
| Adapter provenance requirement | Specific adapters beyond the registry, e.g. an MCP-result extractor helper |
| Opt-in rule and `compute_unavailable` | Gang (fleet) analysis skill: PromQL per-instance grouping, ratio to fleet median, then draw |
| Limits (single source: `plot_table.py`) | Catalog of metrics, sources, and saved queries |

A skill may rely on the contract. The contract never names a skill, and no contract
behaviour depends on a skill being installed.

## 8. Out of scope

- The compute server's own interface, hosting, model list, and its panel features. SPC and
  other advanced models are excluded (section 4.2).
- New adapters beyond those listed as existing.
- The skills and catalog content (companion card).
- Rendering changes to existing panels beyond the histogram-derived heatmap and the
  metric-set layout.

## 9. Follow-up implementation cards (to file after this lands)

1. Histogram-derived heatmap from source buckets (section 5.1).
2. `ccdf` resolvable-tail marker (section 5).
3. Metric set input form, shared-axis layout and limits (section 6).
4. Opt-in field and `compute_unavailable` refusal, with no compute server behind it yet
   (section 4.3).
5. Compute-server interface spec (section 4.1), separate document.
