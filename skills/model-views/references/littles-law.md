# Little's law check: interpretation

## What is compared

Little's law: over a window, mean number in the system L = arrival rate lambda x mean time in
the system W. `check_littles_law` measures all three independently:

- L: the in-flight gauge, averaged over the window (`sum by (by)` of the gauge).
- lambda: `rate()` of a request counter (or a histogram's `_count`).
- W: `rate(_sum) / rate(_count)` of the latency histogram or summary: a MEAN.

R = L / (lambda x W). The check ALWAYS reports the discrepancy first: `discrepancy` holds the
absolute L - lambda W (requests) and the relative R - 1, whole range and per window, each with its
MEASUREMENT interval. Report those numbers whatever the verdict: a `consistent` verdict is context,
never a reason to leave the discrepancy out. `summary` is a ready sentence in that order
(special-cause windows when there are any, discrepancy, verdict and classification, warnings).
The verdict answers only whether L = lambda W holds over the range: `consistent` with promoted
load-peak windows is a valid, intended outcome (an arrivals counter partly compensates in the
peak window) and the promoted windows are the incident; lead with them.

## Sources of variation: say which one

| source | what it is | in the output | what to do |
|---|---|---|---|
| measurement system | the measurement interval: gauge sampling, the steady-state edge straddle, counter scrape timing, the rate() lookback (not on VictoriaMetrics: counters are read as increase() tiles ending at the gauge's scrapes); and a **systematic offset** (persistent L != lambda W across most windows) | `ci95`, `discrepancy.*_ci95`; `classification.systematic` (`source: measurement_system`) | an offset is instrumentation / model mismatch, not the process: unmeasured queueing, latency on a subset or superset, a missing instance, units |
| common cause | the system's inherent variability: at N requests per window L and lambda W legitimately fluctuate +-X% (small systems do not average out), and the windows' own spread | `common_cause` (`rel95`, `spread_rel`, `warning`); windows with `source: common_cause` | do not chase single windows inside the envelope; quote the warning when present |
| special cause | **transient** windows beyond the measurement interval around the reference AND beyond the common-cause envelope; and load-peak windows **promoted** on independent evidence of leaving steady state | `classification.transient` with `source: special_cause`, `phase`, `at_peak`, `load`; `classification.promoted` (`from`, `deviation`, `reason`, `evidence`) | investigate: a load peak (leaving steady state, toward overload), a backlog draining, or a change confined to those windows |

The measurement interval does not contain the counts' Poisson noise: over a window L and lambda W
count the same requests, so that noise is not an error of the comparison. It is the common-cause
scale, reported apart as a warning ("at this traffic (N≈90 completions per window) L and lambda W
legitimately fluctuate +-43% per window; window differences smaller than that are not
distinguishable from small-system behaviour"). Use it to qualify window differences, never to
hide them. Verdicts are Bonferroni-controlled to at most 5% false alarms over all windows and
groups.

## Load peaks: common cause unless there is evidence

A window at a load peak whose deviation is inside the common-cause envelope is **common cause**
by default: say "at a load peak; inside expected fluctuation — not a signal by itself; watch if
it repeats or grows". Do not call it a transition out of steady state on its own. It is
**promoted to special cause** (`classification.promoted`; on a transient, `promoted: true` and
`promotion`) only on independent evidence of leaving steady state — also when its deviation is
inside the measurement interval (an arrivals counter partly compensates, so L ≈ λW even while a
queue builds):

| evidence (`kind`) | what it says | test |
|---|---|---|
| `backlog_growth` | the backlog grew over the window: the gauge's N(end) − N(start), and arrivals − completions when the counter counts arrivals (the smaller of the two) | beyond Cantelli's distribution-free bound on √2·σ_N, σ_N from the gauge outside the peak's episode |
| `latency_rise` | W rose across two consecutive windows into or through the peak (a step to a higher steady load rises once, then holds) | each rise beyond a t threshold on the windows' own standard errors (n_eff df) |
| `peak_growth` | the deviation grows from load peak to load peak (3+ episodes) | Kendall's S, exact one-sided p |

Promotions have their own 5% family-wise budget (a third per evidence type, Bonferroni over
windows and groups). Quote `reason` — which evidence, with its numbers — and cite the
`littles_law_backlog_growth` / `littles_law_latency_rise` / `littles_law_peak_growth` statistics
(value with interval, source special cause). The verdict stays about L = λW: a spike measured
with an arrivals counter can be `consistent` and still have a promoted window — report both.

## Verdicts and what they imply

| verdict | meaning | usual causes (the hints list them) |
|---|---|---|
| `consistent` | no systematic offset, no transient window | none needed; still report the discrepancy and read the assumptions |
| `L_high` | a systematic offset: more in flight than lambda x W explains in most windows | queueing before the latency timer starts (accept queue, pool wait, middleware), requests stuck or leaked (`growing`; the offset is then `drifting`), latency measured on a subset (one route, successes only), a gauge counting broader things (connections) |
| `L_low` | a systematic offset the other way | gauge missing instances, gauge missing short bursts (scrape too coarse), latency on a superset (client or upstream time, retries), arrival counter counting more than the gauge tracks |
| `inconsistent_in_windows` | no systematic offset, but transient windows | read `classification.transient`: `phase: peak` with `source: special_cause` (beyond the envelope or `promoted`: possible transition out of steady state, toward overload — say so explicitly, with the promotion `reason` when there is one), `phase: peak` with `source: common_cause` (not a signal by itself; watch if it repeats or grows), `drain` (recovery after a peak), `other` (a deploy, an instance joining or leaving, a routing or instrumentation change, queueing the timer misses) |
| `insufficient` / `no_traffic` | cannot be judged | too few samples; widen the range or window |

With a systematic offset, transient windows are relative to it (the `reference`), not to 1. A
transient at a load peak is the system, not the instruments: with a counter that counts
completions, a backlog building puts in-flight time in L that completed latencies do not show yet
(`L_high` in the peak window, `L_low` while it drains).

An `L_high` where W is the measured service time can mean the caller waits W plus an unmeasured
queue. If the excess is queueing before the timer, it is L / lambda - W = W (R - 1): state that
conditional estimate with R's interval, never as a measurement; the check cannot tell it from the
other L_high causes (subset latency, broader gauge, leaks, arrivals vs completions) without more
evidence such as a per-instance run or a queue-depth metric. `consistent` means no mismatch
detected at this precision: offsetting errors (subset latency plus broader gauge) can cancel.

## No concurrency signal

The in-flight gauge is optional in real systems. Without it the check cannot be done, and the
tool says so: `check_littles_law` refuses (with the gauge to add), a concurrency query with no
data gives a summary "cannot be checked" and no L, `show_binding` shows a `check` card with the
suggested gauge, `binding_verdict`'s `model_check` is `not_possible`. Say so plainly and record
the gap; never compute L from lambda x W.

## Localise

- Time: `classification.transient` (against the reference), `flagged_windows` (against 1) and the
  per-window rows (`windows`, columns in `window_columns`, each with its `source`), drawn by
  `show(..., mark="littles")`: the discrepancy strip on top (dark band measurement interval,
  light band common-cause envelope, dashed systematic level; special-cause transients shaded,
  common-cause ones hatched, promoted load-peak windows shaded with a bar on top and the reason
  in the hover), L and lambda x W under it.
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
| `window_alignment` | one sub-step grid for all four signals, windows anchored at the requested `start` (never widened), and a second grid shifted by half a window | samples missing in any signal are dropped from all; entries with `grid: offset` were found on the shifted grid (their own `window`): a load episode inside one main-grid window balances over it, so say which grid saw it |
| `warmup` | start-up excluded only if `warmup` is given | pass `warmup="10m"` after a restart or deploy |
| `gauge_sampling` | scrapes see the in-flight count often enough | bursts shorter than the scrape are invisible: L biased low; interval widened but not removed |
| steady state at the edges | requests straddling window edges cancel out | the measurement interval carries the steady-state straddle only; a backlog building or draining inside a window shows as a transient (`phase: peak` / `drain`) |
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

Special-cause windows first when there are any (below: a promoted load peak), then the
discrepancy, the verdict with its source, then warnings: "Over the hour L - lambda W =
+13.3 requests (L 22.9 vs lambda x W 9.54; L / (lambda W) 2.40, +140%, measurement interval +127%
to +152%). Verdict `L_high`: a systematic offset of 2.03 (1.76-2.30) in 10 of 12 five-minute
windows — measurement system: the instruments do not describe the same requests. If the excess is
queueing before the timer, about 1.4 s per request is not covered by the latency timer. Two
windows differ from that level beyond the measurement interval and the windows' own +-43% spread
(special cause, not at a load peak: queue excursions the timer misses, or a change in those
windows). Common cause: at about 2860 requests per window L and lambda W fluctuate +-8%. Assumed:
the counter counts arrivals. Next: bind by instance; check whether the timer starts after the
accept queue." Cite `evidence` (`littles_law_discrepancy`, `littles_law_ratio`,
`littles_law_systematic_offset`, each `littles_law_transient`, and the promotion statistics) in
`finding_create`, with any `input_uncertainty` flag.

A promoted load peak: "Special cause at 25–30 min, promoted from measurement system: at a load
peak the backlog grew +376 requests (gauge +376, arrivals − completions +377), 155× the
steady-state scale (threshold 27, Cantelli): leaving steady state toward overload. Over the hour
L − λW = −0.10 requests (L ÷ λW 0.997, measurement interval −3% to +3%); verdict `consistent`:
Little's law holds overall, which does not make the peak harmless. Check saturation (USE) in that
window." 
