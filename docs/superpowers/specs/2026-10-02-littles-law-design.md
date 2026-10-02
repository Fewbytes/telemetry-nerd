# check_littles_law (czt.2, spec §3.5, §5.1)

Is the measured mean concurrency L consistent with λ·W (throughput × mean latency)? Per window and
overall, with a propagated interval, the assumptions checked and stated, and the mismatch
localised in time (windows) and in series (groups of a shared label).

## Inputs
`check_littles_law(binding? | arrival_rate, latency, concurrency, by?, start, end, window?,
warmup?, latency_unit?, arrivals?)`. A `littles_law` binding (catalog level) supplies the three
metrics and `join_on` (default `by`); explicit roles override it. Each role is a metric name or a
selector `name{matchers}`.

Four queries at one sub-step `s` (bucket end times, `(t-s, t]`), one dataset each:

| quantity | query | per sub-step |
|---|---|---|
| λ | `sum by (G) (rate(A[$__rate_interval]))` (a gauge rate is used as is) | arrivals/s |
| W numerator | `sum by (G) (rate(H_sum[$__rate_interval]))` | latency-seconds/s |
| W denominator | `sum by (G) (rate(H_count[$__rate_interval]))` | completions/s |
| L | `sum by (G) (C)` | time-average of the gauge, with its sample count |

W = Σsum / Σcount over the window: the mean, never a percentile. Native histograms use
`histogram_sum` / `histogram_count`. Latency that is only a percentile (a `quantile=` selector,
`histogram_quantile`, a `_p99` gauge, or no `_sum`/`_count` in the catalog) is refused with a hint
to add a histogram. Rates are per second; W is converted to seconds from the unit (argument,
catalog, else name suffix; if none: assumed seconds and flagged `latency_unit_assumed`).

Windows: `window` (default ≈ range/12, a nice duration) holds k ≥ 8 sub-steps (`s` = the largest
multiple of the scrape interval giving ≥ 20 per window). Sub-steps where any of the four signals is
missing are dropped from all four (alignment); a window with < 4 is `insufficient`. `warmup`
drops the start of the range.

## Estimator and interval (per window, per group, and pooled)
R = L̂ / (λ̂·Ŵ) with L̂, λ̂, S̄, C̄ the sub-step means and Ŵ = S̄/C̄.

1. **Delta method on residuals** (all four quantities, empirical covariance): per sub-step
   z_i = (L_i−L̂)/(λW) − R(λ_i−λ̂)/λ̂ − R(S_i−S̄)/S̄ + R(C_i−C̄)/C̄, Var(R̂) = s²_z·τ_int(z)/n
   (Geyer τ: autocorrelation, n_eff = n/τ). This carries the correlation between L and λW within
   the realisation instead of assuming it.
2. **Floors where the data cannot show the error** (summed linearly = worst-case correlation):
   gauge sampling — scrapes can miss short spikes, so the time average of m samples has at least
   Poisson-occupancy variance max(L̂, λŴ)/m_ind (m_ind = min(samples, T/Ŵ)); λ — Poisson on the
   window's arrivals N; W — mean of C completions with CV ≥ 1. sd = max(delta, floor sum).
3. **Bias bounds added to the half-width**: window edges (requests straddling the boundaries):
   (L_first + L_last)·W_max/(λŴ·T); alignment (the rate window looks back δ = (rate interval −
   s)/2 further than the gauge average): R·δ/T·(relative range of λ, S or C in the window).

Interval = R ± (t_{n_eff−1}·sd + bias), clipped at 0. L and λ·W get their own intervals for the
panel (same terms, per side).

## Verdicts (α = 5% overall, split in two)
- Window (per group): `consistent` | `L_high` | `L_low` from a Bonferroni interval over all
  (group, window) tests at α/2; `insufficient` / `no_traffic` when it cannot be judged. `ci95` is
  the pointwise 95% interval (panel bands, evidence).
- Overall (per group and for the total): pooled over all usable sub-steps at α/2: `L_high` /
  `L_low` when the pooled interval excludes 1; else `inconsistent_in_windows` when any window is
  flagged; else `consistent`. False alarms: ≤ α by construction (Bonferroni + conservative sd).
- `L_high`: time in system not covered by the latency timer (queueing before it starts: accept
  queue, pool wait, middleware), leaked/stuck requests (L−λW grows: trend test), latency on a
  subset of the requests the gauge counts, a gauge counting something broader.
- `L_low`: concurrency missing instances/series, gauge sampling missing bursts, latency measured
  on a superset (client/upstream time, retries), an arrival counter counting more than the gauge
  tracks. A ratio near 10^±3 adds a unit hint (ms vs s).

## Assumptions, checked and stated
- **Steady state**: per window a trend test (stability.trend, n_eff) on L and λ; drift is
  `not_steady` with its size. Little's law holds over any window up to the edge term, which the
  bias bound carries, so drift widens rather than invalidates.
- **Arrivals vs completions**: which one λ counts (`arrivals` argument; auto = completions when the
  counter is the histogram's `_count`, else "unknown, most middleware counts at completion") and a
  flow-balance check of the counter rate against the histogram count rate per window
  (`flow_imbalance`: latency measured on a subset/superset, or backlog growth/drops).
- **Matching label sets / aggregation level**: groups present in some roles only are listed with
  `missing_in`; differing matchers across roles are stated (`selectors_differ`); a `by` label
  absent on a role is reported.
- **Units**: per-second rates; W in seconds with the unit's provenance.
- **Window alignment**: one sub-step grid for all four; the rate lookback offset is a bias term.
- **Warm-up**: excluded only when `warmup` is given; stated either way.
- **Window ≫ W**: `window_short_vs_latency` when Ŵ > T/10.

## Output, evidence, panel
Per group: overall result, windows, flagged windows, assumptions; `total` (all groups summed: what
an ungrouped check sees); `unmatched` groups; `evidence` statistics (`littles_law_ratio`, `L`,
`lambda_W`) through `core.wire.statistic`, citing the concurrency dataset with the other datasets
in params. `show(<concurrency dataset>, question, mark="littles")` draws per group L and λ·W as
window steps with 95% bands, and a ratio strip (1 line, band, verdict colours).

Validation: a seeded discrete-event M/M/c simulation (tests): consistent → false alarms at or
below nominal; unmeasured queueing (timer starts after the queue) → `L_high` in the loaded
windows; a missing instance → `L_low` for the total, localised to that instance (`missing_in:
concurrency`); percentile-only latency refused.
