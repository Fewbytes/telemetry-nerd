# Marks: inputs, preconditions, what to say

`show(dataset, question, mark=...)`. `question` is required. The answer returns `{panel, url,
warnings, auto?, y_range_notes?, reframings?, context?, suggest?}`: read `warnings` and `auto`
before describing the panel.

## Time series

| Mark | Input | Preconditions | Say when citing |
|---|---|---|---|
| `mark="auto"` | series dataset | at most 5 series (the series budget) | step, envelope = min/max within each step, caveats (gaps, settling). A counter selector is drawn as its rate (`auto` says so); `raw=true` draws the running total |
| `mark="spc"` | series, after `analyze` | not a percentile, distribution or raw counter; `windows=[{start, end}]` is the baseline (at most one) | the baseline window, centre and limits, n_eff, which points are signals |
| `mark="seasonal"` | series or distribution, after `compare_seasonal` | enough history (3 usable cycles) | the reference chosen (cycles, timezone, excluded), the band, flagged points |
| `mark="fleet"` | many series of one metric, after `fleet` (or directly) | at least 5 members, one unit, not percentiles | member count, spread, named outliers with kind and effect, churn, missing share |
| `mark="spectrum"` | series | not percentiles or raw counters | only significant peaks with interval; the periods it cannot see (`limits`) |
| `mark="spectrogram"` | series | `segment` between 16 steps and a quarter of the range; `overlap` default 0.5 | the segment length |
| `mark="littles"` | the concurrency dataset from `check_littles_law` | the check ran on it | the discrepancy first (L vs λW per window, ratio strip), then the verdict |

A filtered dataset (`filter`) is drawn with the raw series as context; `view` picks the default
(overlay | filtered | removed | raw). State the filter and why.

## Distributions (from `query_distribution`)

| Mark | Preconditions | Use for |
|---|---|---|
| `mark="heatmap"` (the auto default) | at most 12 series (small multiples) | latency over time: where requests are, how the body and tail move |
| `mark="percentiles"` | `quantiles` 1-4 of 0.5, 0.9, 0.95, 0.99, 0.999 | "p95 over time": per step the SOURCE bucket holding q, drawn only where n >= 10/(1-q); never interpolated |
| `mark="histogram"` | `windows` 1-4 `[{start, end, label}]` | spike vs baseline shape, n per window |
| `mark="ecdf"` | `windows` 1-4 | shares below each edge, window against window |
| `mark="quantile_curve"` | `windows` 1-4 | inverse ECDF as bucket boxes: the honest "percentile curve" |
| `mark="ccdf"` | `windows` 1-4 | tails: P(X > x) on log-log, exact at bucket edges |

The number behind a distribution claim is `fraction_over(dataset, x, start, end)` (exact at a
bucket edge, bounded inside one, Wilson interval, n): cite its `evidence`, draw the heatmap or
ccdf as the picture.

## Code outputs and fits

Code outputs (`code:<node>/<name>`) are drawn as produced, with their declared interval as the
band. A fit is not drawn: show its `_prediction` dataset, or cite its parameters.

## Refused or corrected conventional charts

| Convention | Why it is wrong | What happens instead |
|---|---|---|
| average / max / sum of percentile series, `avg_over_time` of a p99 | a percentile of percentiles is not a percentile; averaging p90s over a day overstated it by 68.5% in the canonical example | refused; aggregate the histogram, take the quantile once |
| 100 lines for 100 pods | nobody can read spaghetti; outliers hide | series budget error; `fleet` band plus named outliers |
| dual y-axis | the visual relation depends on two arbitrary scales | two panels, or `suggest_y_view(panel, mode, label, reason)` with `mode="indexed"` |
| autoscaled y on a flat signal | a 0.2% wiggle fills the panel | reference range by default; zoomed views are labelled |
| p99 from 40 requests | not meaningful (needs n >= ~1000) | drawn only where n suffices; low-n buckets faded |
| interpolating across gaps | invents data | gaps stay gaps; missing data is shown |
| stacked non-additive series (ratios, percentiles) | the stack height means nothing | refused |

An override exists only at explicit user request and renders as a caveat on the panel.
