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

## Discrepancy first (60j, user decision 2026-10-03)
The check always reports the discrepancy, whatever the verdict: per window and pooled, the
absolute L − λ·W (requests) and the relative R − 1 with R = L / (λ·W), each with its measurement
interval. A "consistent" verdict is context, never a reason to hide the numbers. The MCP result
leads with `summary` (text: discrepancy, then verdict and classification, then warnings),
`discrepancy`, `verdict`, `classification`, `warnings`.

## Sources of variation (SPC vocabulary)
Every reported variation is labelled with its source:
- **measurement system** (instrumentation): the measurement interval (gauge sampling, edge
  straddle, scrape timing, rate lookback) and a **systematic offset** (persistent L ≠ λW across
  most windows: unmeasured queueing, subset/superset measured, a missing instance, units). Not
  the process.
- **common cause** (the system's inherent variability): the small-system fluctuation scale (at N
  requests per window, L and λW each fluctuate about ±1.96·(1/√N_arrivals + 1/√N_completions)) and
  the windows' own variation around the reference (3 robust sigma of the window ratios, less what
  the measurement interval explains); the envelope is the wider of the two. Windows inside it are
  not to be chased.
- **special cause** (assignable): **transient** windows beyond the measurement interval around
  the reference AND beyond the common-cause envelope — load peaks, leaving steady state, a change
  confined to those windows; and load-peak windows **promoted** on independent evidence of
  leaving steady state (next section). Investigate.

## Load peaks: common cause unless there is evidence (83w, q2m; user decision 2026-10-03, option C)
A window at a load peak whose L vs λW deviation is inside the common-cause envelope stays
**common cause** by default, worded "at a load peak; inside expected fluctuation — not a signal
by itself; watch if it repeats or grows" (before 83w its label said common cause while the
warning said "possible transition out of steady state"). It is **promoted to special cause**
when independent evidence says the system is leaving steady state — also when its deviation is
inside the measurement interval (an arrivals counter partly compensates: q2m). None of the
evidence uses the deviation's noise envelope:

| evidence | statistic | null (steady state) and test |
|---|---|---|
| (a) growing backlog | N(end) − N(start) over the window: the gauge (reading before the window to its last); when λ counts arrivals also arrivals − r̂·completions (flow balance, r̂ the windows' median counter / count ratio), and the smaller of the two (growth one instrument alone sees is `flow_imbalance`, a measurement question) | mean 0, variance ≤ 2·var(N) (non-negative autocorrelation); var(N) from the gauge in the windows outside the peak's episode (consecutive peak / drain windows) and its neighbours, floored at their mean (Poisson occupancy). Cantelli's one-sided bound P(X ≥ kσ) ≤ 1/(1+k²): distribution-free, so heavy-tailed queue excursions (ρ ≈ 0.95) are covered |
| (b) W rising across consecutive windows | W(i−2) → W(i−1) → W(i) ("into") or W(i−1) → W(i) → W(i+1) ("through"): both rises | each rise beyond a t threshold on the two windows' standard errors of W around their own within-window trend (residual sd / √n_eff, n_eff from the residuals' integrated autocorrelation time; never below W/√completions), Welch–Satterthwaite df. A step to a higher steady load raises W once and holds: one rise is not enough |
| (c) peak-to-peak growth | the largest \|R/reference − 1\| of each load episode, in time order (3+ episodes) | Kendall's S, exact one-sided p (Mahonian distribution) under exchangeability; ties count against the trend |

**Multiplicity**: promotions have their own 5% family-wise budget per check (apart from the
verdict's α), a third per evidence type: (a) Bonferroni over all (group, window) tests; (b) the
same ×2 (two triples), both rises required (the intersection's level is at most either's); (c)
once per group (Bonferroni over groups). The windows tested are those at a load peak; at least 4
windows outside the episode are needed for (a) and (b).

**Output**: the promoted window's `source` becomes `special_cause`; `classification.promoted`
lists each one with `from` (common cause / measurement system), `deviation`
(`within_envelope` / `within_measurement`), `reason` (which evidence, with its numbers) and every
evidence item (value, interval, z and threshold, Cantelli bound or exact p); a promoted transient
carries `promoted` and `promotion`; `classification.promotion` states the levels. The verdict
stays about L = λW (an arrivals-counter spike can be `consistent` with a promoted window). The
reason is in `warnings`, `summary`, `variation` and the panel hover; the significant evidence is
cited as `littles_law_backlog_growth` (requests), `littles_law_latency_rise` (s) and
`littles_law_peak_growth` (relative) statistics with intervals (the readings' own error: counter
scrape timing and the instruments' spread; the windows' standard errors; the windows'
measurement sds), source special cause.

## Estimator and measurement interval (per window, per group, and pooled)
R = L̂ / (λ̂·Ŵ) with L̂, λ̂, S̄, C̄ the sub-step means and Ŵ = S̄/C̄.

The estimand is the realised path: over a window, L·T = ∫N dt and λ·W·T = Σ measured latencies of
the window's completions describe the same requests up to those straddling its edges. So the
interval holds only what the instruments add for THIS window, never how the next window would
differ:

1. **Gauge sampling**: the gauge is read at m scrape instants. Successive differences estimate
   the error (mean(ΔL²)/2 / n), never below a Poisson-occupancy process with correlation time Ŵ
   under the same sampling — max(L̂, λŴ)/m · (coth(x/2) − 2/x), x = scrape/Ŵ — because scrapes can
   miss short spikes even when every sample looks alike.
2. **Edge straddle (steady state)**: requests in flight at an edge put their in-window time in
   L·T and their whole latency in λW·T. In steady state the two edges cancel on average; per
   contiguous segment the remainder has sd 2·W_rms·√max(L, λW) / (λW·T) (Poisson(L) requests per
   edge, residual time second moment 2W², CV 1; W_rms the completion-weighted RMS of the sub-step
   means). A backlog that builds or drains inside a window is NOT in it: that is out of steady
   state, a real (special-cause) discrepancy.
3. **Counter scrape timing**: increments between a window edge and the nearest scrape land in the
   neighbouring window (rate() extrapolates over them): at most one scrape interval g per edge,
   Poisson in that fragment: √(2·g·λ)/(λT) for the arrival counter and √(2·g·C)/(C·T) for the
   latency count/sum (CV 1), added linearly (correlation unknown).
4. **rate() lookback** (bias bound, added to the half-width): each sub-step's rate() looks back
   δ = (rate interval − s)/2 further than the gauge average; the window's counts are those of a
   window shifted by δ: R·δ/T·|x̄_end − x̄_start|/x̄ over the sub-steps one rate interval covers at
   each end (λ, S, C; the largest), plus that estimate's own counting noise √(2/N_end).

1–3 in quadrature on R (independent instruments / instants); interval = R ± (t_{n−1}·sd + bias),
clipped at 0. L and λ·W get their own intervals for the panel. The counts' Poisson noise (±1/√N on
λ and W) was in the interval before 60j; it is not measurement error of the comparison (both
sides count the same realised requests) and is now reported apart as the common-cause scale, with
a warning when it is ≥ 10% per window or when a transient window lies inside it: "at this
traffic (N≈… completions per window) L and λW legitimately fluctuate ±X% per window; window
differences smaller than that are not distinguishable from small-system behaviour".

## Systematic vs transient (α = 5% overall, split in two)
- **Window tests** (Bonferroni over all (group, window) tests at α/2): `consistent | L_high |
  L_low` against 1 (`flagged_windows`), and the transient test against the reference level
  (below). `ci95` is the pointwise 95% measurement interval.
- **Reference and systematic offset**: start from the median window ratio; windows off it beyond
  their measurement interval (combined with the level's) are transient; the level of the rest is
  their equal-weight mean ratio (one heavy window cannot make an offset persistent), its standard
  error the larger of the measurement one and the windows' spread (t with the windows' or the
  sub-steps' df accordingly), tested at α/2. If it excludes 1 — or most windows are flagged on one
  side of 1 — it is the systematic offset and the reference; else the reference is 1. Iterate
  until the transient set is stable. A systematic offset needs most windows (with half or more
  transient there is none). When L − λW trends over the windows (`growing`: a leak, a backlog
  building over the range), the reference is the offset's linear trend: a drifting offset, not a
  run of transients.
- **Transient windows** carry their load context: λ, W and L against the median window, the
  backlog change (gauge at the window's end − start), λ rising; `phase` = `peak` (λ ≥ 1.1× the
  median and in the top quartile, W ≥ 1.5× the median, or a backlog building; special cause —
  beyond the envelope or promoted — is called out as a possible transition toward overload in
  `warnings` and the summary, common cause as "not a signal by itself"), `drain` (a
  backlog draining after a peak) or `other` (a change confined to those windows: a deploy, an
  instance joining/leaving, queueing the timer misses, instrumentation).
- **Overall verdict**: `L_high` / `L_low` when there is a systematic offset; else
  `inconsistent_in_windows` when any window is transient; else `consistent`. False alarms ≤ α by
  construction.
- `L_high`: time in system not covered by the latency timer (queueing before it starts), leaked/
  stuck requests (L−λW grows: trend test), latency on a subset of the requests the gauge counts, a
  gauge counting something broader. `L_low`: concurrency missing instances/series, gauge sampling
  missing bursts, latency measured on a superset, an arrival counter counting more than the gauge
  tracks. A ratio near 10^±3 adds a unit hint (ms vs s).

## Concurrency is optional in real systems
Without a concurrency (in-flight) signal the check cannot be done, and says so plainly with the
suggested gauge (binding gaps / `SUGGESTIONS`): `check_littles_law` refuses a missing role; a
concurrency query that returns no data gives a summary "cannot be checked", no L; a Little's law
binding without concurrency shows a `check` card with the gauge to add, and `binding_verdict`'s
`model_check` is `not_possible`. L is never derived from λ·W.

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
Per group: `discrepancy` (L, λW, difference and relative with measurement intervals, per-window
ranges, common-cause scale), verdict, `classification` (reference, systematic, transient),
`common_cause`, windows (`window_columns`: start, verdict, ratio, interval, L − λW, source),
flagged windows, assumptions; `total` (all groups summed: what an ungrouped check sees);
`unmatched` groups. `evidence` statistics through `core.wire.statistic`, citing the concurrency
dataset with the other datasets in params: `littles_law_ratio`, `littles_law_discrepancy` (pooled
R − 1 with the measurement interval; the absolute difference and the common-cause scale in
params), `mean_concurrency_L`, `lambda_times_W`, `littles_law_systematic_offset` (when present)
and one `littles_law_transient` per transient window (its R − 1, interval, reference, source,
phase), and the promotion evidence (`littles_law_backlog_growth`, `littles_law_latency_rise`,
`littles_law_peak_growth`) of each promoted window. All marked with the inputs' uncertainty status
(mark_statistics, spec §5.3).
`show(<concurrency dataset>, question, mark="littles")` draws per group, first, the discrepancy
strip (L ÷ λW per window, log scale; dark band = measurement interval; light band = common-cause
envelope around the reference; dashed reference when a systematic offset exists, labelled
"systematic offset … (measurement system)"; special-cause transients shaded, common-cause ones
hatched; promoted load-peak windows shaded with a bar along the top, the hover giving the
reason), then L and λ·W as window steps with their bands; the common-cause warning under it.

Validation (seeded discrete-event M/M/c, tests/unit/littles_sim.py; FAR = verdict not consistent):

| scenario (seeds) | before 60j | after |
|---|---|---|
| consistent λ=2 c=4 (150): FAR, pointwise window coverage | 2.0%, 98.7% | 2.0%, 97.7% |
| consistent λ=9.5 c=10 (150) / λ=19 c=25 (50) | 0%, 99.9% / 0%, 98.3% | 0%, 99.6% / 2.0%, 97.3% |
| consistent λ=0.3 / λ=0.05, low traffic (150 each) | 1.3% / 1.3% | 1.3% / 0%; warning ±43% at N≈90 |
| consistent, load steps ρ 0.5→0.925→0.5 (75) | 1.3% | 1.3% |
| gauge ×1.05 / ×1.1 / ×1.2 at λ=9.5 (75): detection | 5% / 95% / 100% | 19% / 96% / 100% |
| gauge ×1.05 / ×1.1 / ×1.2 at λ=2 (75) | 3% / 17% / 83% | 4% / 21% / 84% |
| ρ 1.25 spike for 5 min, completions counter (75) | 0/75 detected | 75/75 transient at exactly the peak (L_high) and drain (L_low) windows |
| hidden queueing only under load (50) | L_high (pooled; not localised) | 49/50 transient in the loaded windows, at the peak |
| hidden queueing at constant ρ 0.95 (50) | L_high | systematic L_high 50/50 |
| missing instance, 1 of 3 gauges (50) | L_low | systematic L_low 50/50, no transients |

After 60j, with an arrivals counter the same spike was mostly invisible (1/75): arrivals ≠
completions while the backlog builds and the instruments partly compensate. Real rate() lookback
(not in the simulation) adds a large bias bound in a window where the latency sum's rate changes
fast, so a spike can be within the interval there — the promotion evidence does not depend on it.

Load-peak promotion (83w, q2m; seeded littles_sim runs, seeds 1000+; "special" = the window's
source is special cause, beyond the envelope or promoted):

| scenario (seeds) | before 83w | after |
|---|---|---|
| consistent λ=2 c=4 (150) / ρ 0.95 λ=9.5 c=10 (150) / ρ 0.95, completions counter (75): runs with any promotion | — | 0 / 0 / 0 |
| queueing the timer misses at ρ 0.95 (50): heavy-tailed excursions, some at a "peak" | 37 runs with special-cause windows | unchanged; 0 promotions (common-cause peaks stay common cause) |
| load steps ρ 0.5→0.925→0.5 (75) | 2 runs with a special window | 2; 0 promotions (a first, baseline-scaled W test promoted 39/75: replaced by the consecutive-rise test) |
| low traffic λ=0.3 / λ=0.05 (150 each) | 0 | 0 promotions |
| ρ 1.25 spike 5 min, completions counter (75): peak window special | 49/75 (the rest common cause) | 75/75 (26 promoted on backlog growth) |
| ρ 1.25 spike 5 min, arrivals counter (75): peak window special | 1/75 | 75/75 (74 promoted on backlog growth, gauge and flow balance agreeing); the same with the counter's semantics unknown (gauge alone) |
| overload ρ 1.05 for 15 min, arrivals counter (75): any of its windows special | 0/75 | 71/75 (backlog growth in 68, W rising across consecutive windows in 46) |
| six peaks ρ 0.8→1.05 every 20 min, queueing the timer misses (50): last three peaks all special | 42/50 | 44/50 (15 runs promote on peak-to-peak growth) |
| six peaks ρ 0.6→0.9, same (50): last three peaks all special | 0/50 | 12/50 (13 runs promote on peak-to-peak growth: a distribution-free trend over 6 points at 1.7% has little power) |
