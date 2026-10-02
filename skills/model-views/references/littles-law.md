# Little's law check: interpretation

## What is compared

Little's law: over a window, mean number in the system L = arrival rate lambda x mean time in
the system W. `check_littles_law` measures all three independently:

- L: the in-flight gauge, averaged over the window (`sum by (by)` of the gauge).
- lambda: `rate()` of a request counter (or a histogram's `_count`).
- W: `rate(_sum) / rate(_count)` of the latency histogram or summary: a MEAN.

R = L / (lambda x W) with a 95% interval. R near 1 means the three instruments tell one story.
The interval combines the gauge's sampling error (scrapes, floored by what a Poisson occupancy
process would give), the counts' Poisson errors, and bias bounds for requests straddling window
edges and the rate lookback. Verdicts are Bonferroni-controlled to at most 5% false alarms over
all windows and groups.

## Verdicts and what they imply

| verdict | meaning | usual causes (the hints list them) |
|---|---|---|
| `consistent` | interval contains 1 | none needed; still read the assumptions |
| `L_high` | more in flight than lambda x W explains | queueing before the latency timer starts (accept queue, pool wait, middleware), requests stuck or leaked (trend in L - lambda W), latency measured on a subset (one route, successes only), a gauge counting broader things (connections) |
| `L_low` | less in flight than explained | gauge missing instances, gauge missing short bursts (scrape too coarse), latency on a superset (client or upstream time, retries), arrival counter counting more than the gauge tracks |
| `inconsistent_in_windows` | pooled R is fine but some windows are flagged | a transient: read `flagged_windows` |
| `insufficient` / `no_traffic` | cannot be judged | too few samples; widen the range or window |

An `L_high` where W is the measured service time is the classic finding: the caller waits W plus
an unmeasured queue. Compute the implied unmeasured time as L / lambda - W and state it as an
estimate with R's interval, not as a measurement.

## Localise

- Time: `flagged_windows` and the per-window rows (`windows`), drawn by `show(..., mark="littles")`.
- Series: re-run with `by=["instance"]` (or the join label). `groups` give a verdict per member;
  `unmatched` lists groups missing from a signal (`missing_in: concurrency` is a missing
  instance in the gauge and explains `L_low` for the total).
- `total` is the ungrouped view: what a check without `by` sees.

## Assumptions (each is `ok`, `assumed` or `flagged`)

| name | what it asks | when violated |
|---|---|---|
| `steady_state` | L and lambda not drifting inside windows | drift is `not_steady` with its size: the edge term grows, intervals widen; do not read a single window as an equilibrium |
| `arrivals_vs_completions` | which event the counter counts (`arrivals=auto\|arrivals\|completions`) | a flow-balance check compares the counter's rate with the histogram count's: `flow_imbalance` means latency on a subset or superset, or backlog growth/drops. `assumed` for a plain counter: most middleware counts at completion |
| `label_sets` | the three signals describe the same entities | differing matchers (`selectors_differ`) or groups missing from a role: the ratio compares different populations |
| `units` | W in seconds, rates per second | ms vs s mismatch gives R near 1000 or 0.001: pass `latency_unit` (`s\|ms\|us\|ns`) |
| `window_alignment` | one sub-step grid for all four signals | a bias term; samples missing in any signal are dropped from all |
| `warmup` | start-up excluded only if `warmup` is given | pass `warmup="10m"` after a restart or deploy |
| `gauge_sampling` | scrapes see the in-flight count often enough | bursts shorter than the scrape are invisible: L biased low; interval widened but not removed |
| window vs W | window much longer than W (`window_short_vs_latency`) | the straddling-requests bias is large: use a longer `window` |

An `assumed` entry is a statement to repeat in the answer ("the counter was assumed to count
completions"), not a pass.

## Units pitfalls

- A histogram named `..._milliseconds` or with a catalog unit is converted. Without a known unit
  the check assumes seconds and flags `latency_unit_assumed`: if R is about 1000 or 0.001, it is
  almost surely a unit mismatch, not a service finding. Fix with `latency_unit`.
- The arrival counter is rate-converted per second; a gauge is used as is.
- OTel HTTP histograms are seconds; Prometheus client histograms usually seconds; some
  exporters use ms.

## Why percentiles are refused

L = lambda x W holds for the mean. A p99 or a median is not the mean, percentiles do not add or
average across series or time, and a ratio built from them is not a test of anything. The tool
refuses `quantile=` selectors, `histogram_quantile`, `_p99`-style gauges and metrics with no
`_sum` / `_count` in the catalog, with a hint to record a histogram. If only percentiles exist,
say that the check cannot be run and record a gap.

## Reporting

"Pooled L / (lambda W) = 2.40 (95% interval 2.26-2.54), `L_high`; every 5-minute window flagged.
Measured W is 1.0 s while L / lambda is 2.4 s, so about 1.4 s per request is not covered by the
latency timer. Assumed: the counter counts arrivals (flagged: assumed). Hints: queueing before
the timer starts, latency on a subset, a broader gauge. Next: bind by instance and compare, check
whether the timer starts after the accept queue." Cite `evidence` (`littles_law_ratio`, `L`,
`lambda_W`) in `finding_create`, with any `input_uncertainty` flag.
