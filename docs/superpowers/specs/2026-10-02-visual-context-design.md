# Visual context encoding (2as.15)

A number on a chart means little without what bounds it, what is known good or bad, and what it
should be compared with. The catalog already holds this; panels now draw it.

## Context kinds
One resolver (`WorkspaceService.catalog_context_specs`) returns specs, each with `origin`,
`confidence` and `basis` (no unexplained numbers; a bound without provenance cannot be stored):

- **Hard limit**: relation `bounded_by`. Joins the reference y range (2as.10).
- **Threshold**: relation `threshold_by` (metric-valued: sensor critical, request) or catalog field
  `thresholds` (`[{value, label, tone, direction}]`, a claim per origin like any field). Drawn as a
  dashed line by tone; never stretches the y axis, so a far-off threshold cannot flatten the data.
- **Reference series**: relation `same_quantity` (client vs server, upstream vs downstream), faint
  dashed, at most two. The last-week ghost and fleet median are unchanged.
- **Reframing**: proposed, never silent (below).

Limits first, then thresholds, then references; at most six lines per panel.

## How a target lines up
Relation params (validated, pack-expressible as `limits`/`thresholds` entries):
`join_on` (labels both sides share; default all), `matchers` (fixed target labels, e.g.
`resource="memory"`), `applies_to` `level|rate` (a bound on `rate(metric)` such as link speed does not
apply to the running total, and the reverse), `zero_is_unlimited` (the target is filtered `> 0`, so
"no limit" draws nothing, including cAdvisor's -1 quota), `expr` (a derived target such as
`quota / period`; every metric in it must exist in the source, checked on write and when a pack
relation is applied), `tone`, `label`. The panel's own selector matchers are narrowed to `join_on`
and attached to the target. A failing or empty target is a note on the panel, never an error.

## Reframing
Pack `[[reframe]]` rules (`MemFree`→`MemAvailable`, `filesystem free`→`avail`, each with the reason)
apply when the source has the replacement; a generated "% of <limit>" appears for a plain selector with
a same-label hard limit. They are stored on the panel's y context, shown as buttons with the reason on
hover, and returned by `show` to Claude. Accepting one (`POST /api/panels/{id}/reframe`, MCP `reframe`)
creates a NEW panel over the same time range and step, marked `auto.transform = "reframe"` with a
caveat on it; the original is untouched.

## Gaps
When a pack expects a target the source does not export (no `node_network_speed_bytes`), charting the
metric raises one gap per (metric, target) that recommends exporting it (stored with the binding-gap
table as kind `context`).

## Packs
node_exporter: memory by `MemTotal`; filesystem by size; fds, conntrack by their limits; network
rate by link speed; hwmon temperatures with sensor max and critical; the two reframings.
kubernetes: container memory working set by the cAdvisor limit (+ a request line); container CPU rate
by quota ÷ period.

## Not done
SLO and error-budget burn lines (need an SLO model), queueing knee and time-to-full (M4 fits), shaded
zones, peer-median references beyond fleet panels, node CPU core-count bound. Cert expiry, lag windows
and similar are already expressible as `thresholds` claims.
