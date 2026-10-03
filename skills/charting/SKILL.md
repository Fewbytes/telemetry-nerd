---
name: charting
description: This skill should be used when choosing how to draw telemetry in Telemetry Nerd: before calling show, show_binding, suggest_y_view, set_overlays, show_marginal or reframe, or when asked to "chart this", "graph latency", "plot the p99", "show all pods", "compare with last week", "show the distribution", "heatmap", "why does this chart look flat", "zoom the y axis", "log scale", "dual axis", "small multiples", "which chart", "put these on one chart". Gives a decision table of which view answers which question.
---

# Charting: which view answers which question

Every panel answers one explicit question (`show(dataset, question)`); phrase the question
first, then pick the view that answers it. The chart is evidence only for that question.

## Binding rules

- **Never average percentiles**, across series or time. Aggregate the histogram, then draw
  the distribution or take the quantile once. A percentile series is refused by fleet, SPC
  (`analyze`), spectrum and filters; `compare_seasonal` re-fetches the histogram per cycle
  (summary quantiles are refused).
- **Histograms first** for latency: `query_distribution` then a heatmap; percentiles only on
  request, drawn as the bucket that holds q and only where n >= 10/(1-q).
- **Series budget**: at most 5 lines; distributions as at most 12 small multiples; many members
  of one metric is a fleet (band plus named outliers), never spaghetti.
- **No dual y-axes**: series of different scales go to an indexed y-view or to two panels.
- **Units and bounds are checked**: pass `unit` only when it can be vouched for (ratio vs %, s
  vs ms); fix a wrong unit with `catalog_write`, never by relabelling. `bounds_lo` /
  `bounds_hi` assert natural bounds of a derived expression (0 and 1 for a fraction); they
  override the catalog, so assert only what physically holds.
- **Counters are drawn as rates**: `show` on a counter selector draws a new rate dataset and
  returns it as `drawn_dataset`; run `analyze`, `spectrum`, `fleet` and `filter` on that
  handle, not on the counter (they refuse raw counters).
- **Look, then claim**: a chart catches wrong joins, gaps and units before a statistic does.

## Decision table

| Question | Data | Call |
|---|---|---|
| What does this signal do over time? | `query` (counter selectors are drawn as rates) | `show(dataset, question)` (auto: mean line + min/max envelope) |
| How are latencies distributed over time? | `query_distribution` | `show(dataset, question)` (auto: heatmap) |
| How did the p50 / p95 move (only where n suffices)? | distribution | `show(dataset, question, mark="percentiles", quantiles=[0.5, 0.95])` |
| Spike vs before: did the distribution change shape? | distribution | `show(dataset, question, mark="histogram", windows=[spike, baseline])`, or `mark="ecdf"` / `mark="quantile_curve"` |
| How heavy is the tail? | distribution | `show(dataset, question, mark="ccdf", windows=[...])` |
| What share of requests was slower than X? | distribution | `fraction_over(dataset, x)` (the number), then the heatmap or `ccdf` as the picture |
| Is it stable, shifted, drifting, in control? | series, after `analyze` | `show(dataset, question, mark="spc")` |
| Unusual for this hour / weekday? | series, after `compare_seasonal` | `show(dataset, question, mark="seasonal")`; latency: cite `share_over.evidence`, draw now's heatmap and a cycle's dataset with `mark="histogram"` |
| Which pods / nodes are off? (many members) | series, after `fleet` | `show(dataset, question, mark="fleet")` |
| What periods does it contain? | series | `show(dataset, question, mark="spectrum")` |
| Do periods come and go? | series | `show(dataset, question, mark="spectrogram", segment="30m")` |
| Do concurrency, throughput and latency agree? | the concurrency dataset of `check_littles_law` | `show(dataset, question, mark="littles")` |
| Is the service healthy, what moved first? | a binding | `show_binding(source, kind, key, range)` to look (one panel group, shared time axis), then `binding_verdict(group=pgN)` for the answer |
| Successes vs failures latency | histogram-backed dataset | `split_outcome(dataset)` |
| Trend without noise / spikes without the baseline | series | `filter(dataset, kind, period, reason)`, then `show` |
| Is now's value distribution different from before? | a time panel | `show_marginal(panel, reference="previous" \| "week" \| "profile")`; profile: plain series, step at most 1h, both sides hourly means; say requests vs per-step samples, with n |
| Same window last week? | a time panel | `set_overlays(panel, ghost=true)`, `show_marginal(panel, reference="week")`, or `suggest_y_view(panel, mode, label, reason, baseline)` with `mode="indexed"`, `baseline="week"` |

Details per mark (requirements, refusals, what each draws): `references/marks.md`.

## Y-views and overlays

The default y range is the **reference range** (data, normal range, physical limit), so a small
wiggle on a large signal looks small. Offer other views; the user picks:

- `suggest_y_view(panel, mode, label, reason)`, at most 4 per panel, `reason` one line:
  `mode="meaningful"` (percentile panels: drop low-n buckets that squash the axis),
  `mode="log"` (positive data over more than 2 decades), `mode="indexed"` with
  `baseline` window | previous | week (compare different scales; never a dual axis),
  `mode="zero"`, `mode="data"` (zoomed, always labelled), `mode="band"` (lo..hi),
  `mode="typical"` (observed p1-p99 from `catalog_scan`); also `mode="reference"` and
  `mode="semantic"` (natural bounds). A fleet panel takes only zero, data, reference, semantic
  and band.
- `set_overlays(panel, normal, limit, ghost)`: the seasonal normal band, the physical limit line
  (a `bounded_by` metric), last week as a faint ghost. Only the flags passed change; normal
  and limit default to on where the data exists, ghost to off (it fetches last week).
- `reframe(panel, index)`: accept a proposed reframing (available instead of free memory, used
  as a share of its limit). Propose it in the reply first.
- Never re-query to hide an outlier: change the view, keep the data.

## Panel groups and fleet views

`show_binding` draws a RED / USE / Little's law binding as one group (`pgN`): one range and step,
linked crosshair, each role in its natural form (rates, error ratio with a Wilson band, latency
heatmap, utilization on 0..1, saturation with its limit). A fleet panel offers the user three
views: band + outliers (default), member x time heatmap, small multiples of the top outliers;
say which one shows the point being made.

## Render budget

The server sends about one bucket per pixel (min/max preserving). Keep `step="auto"` unless the
question needs a resolution; a fine step over a long range breaks the budget (about 2 points
per pixel, 100 ms), which is logged as `render_budget_exceeded` and drawn coarser. Never ask for
a step below two scrape intervals: it invents resolution (`fake_resolution` caveat).

## After drawing

Share the panel id (`p5`) and `highlight(object, note)` it; read `warnings`, `auto` (what was
transformed) and `y_range_notes`. Mark onsets and deploys with `annotate`. A chart supports a
finding as `{kind: "panel", panel}` evidence only for its own question (see `evidence`).

## Additional resources

- **`references/marks.md`**: every mark with its inputs, preconditions, refusals and what to
  say when citing it; the conventional charts that are refused and why.
