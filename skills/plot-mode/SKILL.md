---
name: plot-mode
description: This skill should be used when the user wants numbers drawn that Claude holds itself rather than a metric from a source: "plot these numbers", "chart this table", "draw this CSV", "compare these two samples", "q-q plot", "bar chart of these counts", "show the shares", "pie chart", "donut". Says when to use plot_data and plot_csv instead of query + show, which kind answers which question, why pie charts are refused, and the size limits.
---

# Plot mode: drawing data you supply

Plot mode draws numbers the caller passes in: a computation, a table from the conversation, a
CSV. Nothing is queried and nothing is re-fetched; the panel is labelled as supplied by Claude and
`unit` is recorded as "provided by claude". `question` is required on every call.

## Which entry

| You have | Use |
|---|---|
| A metric in a connected source | `query` then `show` (charting skill), never plot mode |
| Numbers you hold: series with timestamps, arrays, labelled columns, a matrix | `plot_data` |
| The same numbers as CSV text | `plot_csv` |

A plot-mode request is ad hoc by nature ("show me a graph of these numbers"): pass
`target="charts"` so the panel lands on the Charts page and the user's active investigation stays
as it is. The reply's `url` points at it. If the user then wants to dig into it, call
`workspace_promote(panel)`: the new investigation carries the panel as an illustration only,
never as evidence.

Plot mode never replaces an investigation: data typed in by Claude is not evidence about the
source. If the numbers came from `query`, show the dataset itself.

The spec's separate plot_values entry does not exist: `plot_data` takes `kind` and `data`
for every non-time-series form.

## Which kind

| Question | `kind` |
|---|---|
| How does it move over time? | `line` (`series`, or `data` with a `ts` column) |
| What shape is the sample? | `histogram`, `ecdf` |
| Do two samples differ, and where? | `qq` (exactly 2 columns) |
| How heavy is the tail? | `quantile_curve`, `ccdf` |
| How large is each category? | `bar` (one column plus `labels`) |
| Several series per category | `bar_grouped` (2 to 4 columns sharing `labels`) |
| A matrix of values | `heatmap` |

Bars are sorted descending with a zero baseline, never truncated, never log. Missing values
(`null`, empty CSV cell) are gaps in lines, dropped and counted in distributions, "no value" in
bars: never zero.

## No pie charts

Pie, donut, radial, sunburst and 3-D forms are never rendered, with no flag or override. A
`kind` of `pie`, `donut`, `doughnut`, `radial`, `sunburst` or `3d_pie` fails with
`refused_form` and nothing is drawn. Do not retry with another spelling. For parts of a whole,
call again with `kind="bar"` and state the total in the question. For parts over time, use
lines or one panel per part; stacked areas are not offered either.

## Limits

Over a limit the call is refused with `over_limit`; nothing is truncated for you. Split or
aggregate first, then call again.

| Limit | Value |
|---|---|
| Lines per plot | 5 (6 to 8 columns: one call per group of 5; more than 8 is refused) |
| Samples per distribution plot | 4 |
| Points per time series | 100 000 |
| Values per sample | 1 000 000 |
| Bars | 50 |
| Heatmap | 100 rows by 100 columns |
| CSV text | 5 MiB, comma delimiter only, no file or URL input |
| Label length | 80 characters |

Values must be finite numbers: `NaN` and `Inf` are `not_finite`. Time is epoch milliseconds or
ISO-8601 with timezone, strictly increasing. A refusal opens with its code (`refused_form`,
`over_limit`, `not_finite`, `ts_not_increasing`, `compute_unavailable`) and names the series or column and the index
that failed.

Advanced model features are off unless you pass `compute` naming one. No compute server exists
yet, so any `compute` value fails with `compute_unavailable` and nothing is drawn; drop the field
to draw the plain chart. A server being attached would not opt you in.
