# Auto-charting by semantics (2as.14)

`show` picks the form of a signal from what the catalog knows; histogram heatmaps and Wilson ratio
bands stay in M4 (4ok).

- **Counter -> rate.** `TelemetryService.show_auto` (used by the MCP `show` tool and `POST /api/show`;
  the sync `show` is unchanged): when the dataset is a plain selector of a metric whose resolved type
  (`catalog_facts`: user > claude > stats > pack > metadata > rule, name rule included) is `counter`,
  a new dataset `rate(<selector>[$__rate_interval])` is queried over the same window, step and
  source, and the panel is drawn from it. The dataset that was asked for is untouched and cited in
  `spec.auto {transform, source_dataset, reason}`; the panel shows "Shown as a rate: ... the running
  total is dataset dN; ask Claude for it with show(raw=true)". `raw=true` draws the dataset as is.
  If the rate query fails the raw chart is drawn.
- **Cannot be rewritten -> warn.** Expressions using a counter outside rate-like calls
  (`sum(counter)`, ...) cannot be rewritten safely: `show` adds a `raw_counter` warning telling the
  caller to chart rate(...[$__rate_interval]) (suppressed for `raw=true`).
- **Bounded gauge -> fixed axis.** Client `auto` resolves to `semantic` when the catalog bounds the
  metric on both sides ([0,1], [0,100]) and the drawn data fits inside: the axis is the metric's
  natural bounds, labelled "natural bounds [0,1] (auto)". Data outside its bounds falls back (the
  contradiction is shown, not clipped). It outranks auto-log; a one-sided bound (>= 0) is unchanged.
- Units already come from the catalog (2as.4); a rate panel's unit is `count/s` etc.

Out of scope: rewriting arbitrary expressions, heatmaps for histograms and ratio bands (M4).
