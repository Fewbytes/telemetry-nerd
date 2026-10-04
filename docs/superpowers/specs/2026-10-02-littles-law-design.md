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

| quantity | query (VictoriaMetrics) | query (Prometheus) | per sub-step |
|---|---|---|---|
| λ | `sum by (G) (increase(A[r]))` / r | `sum by (G) (rate(A[$__rate_interval]))` | arrivals/s |
| W numerator | `sum by (G) (increase(H_sum[r]))` / r | `… rate(H_sum[$__rate_interval])` | latency-seconds/s |
| W denominator | `sum by (G) (increase(H_count[r]))` / r | `… rate(H_count[$__rate_interval])` | completions/s |
| L | `sum by (G) (C)` | same | time-average of the gauge, with its sample count |

(a gauge rate is used as is.) **Counters on VictoriaMetrics (9fd, 2026-10-03)**: `r` is the
series interval (the scrape interval, for scraped metrics) and each sub-step averages the subquery evaluated every
`r`: VictoriaMetrics' `increase()` counts from the last sample before its window to the last one
in it, without extrapolation, so the `r` tiles partition the counter exactly (their sum over a
sub-step is the counter's increase between the last scrapes before its two ends; verified on
v1.137) and each tile ends at the very scrape the gauge is read at. No lookback by construction.
With `rate(x[$__rate_interval])` (the Prometheus path, and what the check did before) each
evaluation reaches back over the whole query window: measured on VM, the counters' window sits
ri/2 behind the bucket (10 s at 5-60 s sub-steps with a 5 s scrape) — the old bias term assumed
(ri − s)/2, i.e. 2.5 s at 15 s sub-steps — and the spike's counts are smeared into the next
windows (below).
A tile in which no scrape landed (scrapes on the tile edges with jitter: the next tile holds
two) comes back without a value — the source drops buckets without an observed sample — and is
taken as what VictoriaMetrics returns for it: an increase of 0, the gauge's last reading. Left
out, the next tile's two intervals of counts were set against one gauge reading (λ 1.4-1.6×, an
`L_low` "systematic offset" in every window at that scrape phase). Only isolated gaps; longer
ones are missing data in all four signals.

W = Σsum / Σcount over the window: the mean, never a percentile. Native histograms use
`histogram_sum` / `histogram_count`. Latency that is only a percentile (a `quantile=` selector,
`histogram_quantile`, a `_p99` gauge, or no `_sum`/`_count` in the catalog) is refused with a hint
to add a histogram. Rates are per second; W is converted to seconds from the unit (argument,
catalog, else name suffix; if none: assumed seconds and flagged `latency_unit_assumed`).

Windows: `window` (default ≈ range/12, a nice duration) holds k ≥ 8 sub-steps (`s` = the largest
multiple of the series interval giving ≥ 20 per window). Sub-steps where any of the four signals is
missing are dropped from all four (alignment); a window with < 4 is `insufficient`. `warmup`
drops the start of the range. **Windows are anchored at the requested start** (rounded up to the
sub-step; xa4): before, the range was widened outward to wall-clock multiples of the window, so
up to a window of data before `start` (a warm-up the caller excluded) was judged and the grid
did not follow the data. A trailing part of ≥ half a window is judged as a short window; a range
holding fewer than two windows is refused. **Series interval probed**: the gauge's median sample
spacing at the range end is compared with the source's configured series interval; a mismatch is
stated in `gauge_sampling` (and in a refused window's message, with the `source_connect(…,
resolution=…, replace=true)` hint); the error terms use the larger.

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
- **undetermined** (4ahp, principle 16): with fewer than 5 judged windows the windows' own
  variation cannot be estimated, so the small-system scale alone would rest on Poisson arrivals
  and latency CV 1 (optimistic under bursty arrivals or slow requests in clusters). The label
  then rests on a **cautious envelope**: each small-system term inflated by the window's own
  sub-steps — sqrt of the arrival counts' long-run variance-to-mean ratio, and the effective
  latency CV from e = latency-seconds − W × completions per sub-step — around the window's linear
  trend (a ramp inside the window is the change judged, not its noise; `not_steady` flags it),
  × the residuals' autocorrelation time, never below Poisson. Beyond it: special cause; beyond
  the Poisson envelope only: undetermined, both envelopes in the transient's `envelope`
  (`label_rests_on: cautious`); option C promotions may still make it special.
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
R = L̂ / (λ̂·Ŵ) with L̂, λ̂, S̄, C̄ the sub-step means and Ŵ = S̄/C̄. **Gauge end-of-interval
reading (trapezoid)**: the counters cover (s_{j−1}, s_j], the gauge is read at s_j, so the sum of
readings integrates N half a scrape late: off by (N_end − N_before)·g/2 per contiguous segment
(exact for a linear path). Its sign is known, so it is corrected, not bounded: L̂ −= (N at the
window's last sub-step − N at the sub-step before it)·g / (2T). On the seeded spike runs it cuts
the rms error of L̂ against the exact ∫N dt from 14.3% to 12.7% (mean error ~0 either way).

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
   neighbouring window (rate() extrapolates over them): at most one series interval g per edge,
   Poisson in that fragment: √(2·g·λ)/(λT) for the arrival counter and √(2·g·C)/(C·T) for the
   latency count/sum (CV 1), added linearly (correlation unknown).
4. **rate() lookback** (bias bound, added to the half-width; Prometheus path only — 0 with VM's
   increase() tiles): each sub-step's rate() looks back δ = (query window − series interval)/2 further
   than the gauge reading; the window's counts are those of a window shifted by δ:
   R·δ/T·|x̄_end − x̄_start|/x̄ over the sub-steps one query window covers at each end (λ, S, C;
   the largest), plus that estimate's own counting noise √(2/N_end). On real VM data this bound
   was the widest term exactly where a spike is (0.38 on a peak window whose sd was 0.07) and still
   failed to cover the truth (below): the tiles remove the cause instead of bounding it.

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
- **Shifted grid (xa4)**: a load episode (backlog building, then draining) that starts and ends
  inside one window balances over it — Little's law holds over that window — so where it falls
  on the grid decided whether it was seen (real VM, 2-minute windows: a ρ 1.5 × 60 s spike was
  missed at 1 of 3 grid phases with either counter). The windows are also judged on a second grid
  shifted by half a window; the window tests and the promotions are Bonferroni over both grids
  (2 × tests). Special-cause windows of the shifted grid that no special-cause window of the main
  grid overlaps are added to `transient` / `promoted` with `grid: offset` and their own span,
  ratio, interval and reference (panel: shaded by their own span); a non-promoted one makes the
  verdict `inconsistent_in_windows` when the main grid alone is consistent. An episode shorter
  than half a window can still fall inside a window of both grids. Cost (seeded, below): a change
  filling exactly one main-grid window is found a little less often (×1.6: 52 → 45 of 75), a
  15-minute overload 71 → 64 of 75; a 90 s spike anywhere on the grid 28 → 46 of 75.
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
- **Window alignment**: one sub-step grid for all four, windows anchored at the requested start;
  VM counters as increase() tiles ending at the gauge's scrapes (no lookback); elsewhere the rate
  lookback offset is a bias term; the gauge's end-of-interval reading corrected; a load episode
  that starts and ends inside one window balances over it (stated).
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

Real-VM fixes (9fd, xa4, 00s; 2026-10-03): VM increase() tiles, windows anchored at `start`,
the gauge's end-of-interval reading corrected, the shifted grid. Seeded calibration
(`scripts/calibrate_littles.py`, fixed seeds; before = master at 0e6833d; the analysis layer, so
the tiles are not in it — the sim's counters are exact per scrape already):

| scenario (seeds) | before | after |
|---|---|---|
| consistent λ=2 c=4 (150): FAR, coverage, promotions | 3/150, 97.7%, 0 | 3/150, 97.9%, 0 |
| consistent λ=9.5 c=10 (150) | 0/150, 99.4%, 0 | 0/150, 99.8%, 1 promotion |
| consistent λ=19 c=25 (50) | 1/50, 98.0% | 0/50, 98.2% |
| consistent λ=0.3 (150) | 1/150, 98.8% | 1/150, 98.8% |
| load steps ρ 0.5→0.925→0.5 (75): alarms, promotions | 1, 2 | 1, 1 |
| gauge ×1.05 / ×1.1 / ×1.2 at λ=9.5 (75) | 14 / 73 / 75 | 12 / 73 / 75 |
| gauge ×1.05 / ×1.1 / ×1.2 at λ=2 (75) | 2 / 17 / 63 | 1 / 17 / 63 |
| gauge ×1.3 / ×1.6 in exactly one main-grid window (75) | 10 / 52 (no shifted grid) | 2 / 45 |
| overload ρ 1.05 15 min, arrivals counter (75): any window special | 71 (no shifted grid) | 64 |
| 90 s spike ρ 1.5 at a seeded position, completions / arrivals counter (75): a special window at it; stray special windows | 28 / 27; 0 (no shifted grid) | 46 / 44; 0 |
| ρ 1.25 spike 5 min, completions counter (75): peak window special | 75 | 75 |
| ρ 1.25 spike 5 min, arrivals counter (75): peak window special (promoted) | 75 (75) | 75 (75) |
| six peaks ρ 0.6→0.9, queueing the timer misses (seeds 900-949): promoted on peak-to-peak growth | 10/50 | 8/50 (part of it was the gauge's end-of-interval bias) |

Fewer than 5 windows (4ahp; `calibrate_littles.py`, four 5-minute windows on 20 minutes of
M/M/c; before = the Poisson small-system envelope alone, after = the cautious envelope; ≥ 5
windows unchanged, every row above identical):

| scenario (seeds) | before: special | after: special / undetermined |
|---|---|---|
| steady λ=2 c=4, Poisson arrivals, service CV 1 (150) | 0 | 0 / 0 |
| steady λ=9.5 c=10, Poisson (150) | 0 | 0 / 0 |
| steady λ=2 c=4, arrivals in batches of ~5 (index of dispersion ~9) (150) | 9 (6%) | 3 / 2 |
| steady λ=9.5 c=10, batches of ~5 (150) | 0 | 0 / 0 |
| steady λ=2 c=4, service CV 3 (lognormal) (150) | 1 | 0 / 0 |
| ρ 1.25 spike 5 min in window 2, completions counter (75): peak special | 73 | 69 / 4 |
| same, arrivals in batches of ~5 (75) | 60 | 50 / 10 |

(With an arrivals counter the spike's window stays near R = 1 and its special cause is a
promotion, which needs 4 baseline windows: not possible with 4 windows, before or after.)
The real-VM integration tests (`tests/integration/test_queue_sim_vm.py`, 1m / auto windows,
≥ 5 per grid) pass before and after (19/19).

**Real VictoriaMetrics validation** (bead 317; `scripts/validate_queue_sim.py`: VM v1.137.0 scraping
the queue-sim exporters every 5 s over HTTP, 3 pods, `by=["pod"]`, 20 s warm-up excluded; "exact
R in interval" = windows whose 95% measurement interval holds the simulation's exact R, from
`queue_sim.exact_windows`). The first runs (and the earlier notes on 317) were taken through the
podman host gateway, which stalled scrapes for 1-19 s and dropped whole minutes (the spike's
window was `insufficient`); the exporters now run in a container on the VM's network, addressed
by IP (≤ 1 failed scrape per scenario). Ground truth: leak = `L_high` with `growing` (a drifting
offset, as above; the scenario said `inconsistent_in_windows`); systematic scenarios may carry
transients of the same fault in its own direction (hidden-queue excursions); a spike = a
special-cause window at its episode (load + drain), none elsewhere, `peak`/`drain` labels on
the right side. Before = master 0e6833d (rate() lookback, wall-clock windows); after = this fix.

| scenario | window | before: result (exact R in interval) | after: result (exact R in interval) |
|---|---|---|---|
| consistent ρ 0.8 | 1m / 2m / auto (1m) | consistent ×3 (all) | consistent ×3 (10/10, 5/5, 10/10) |
| hidden queueing ρ 0.9 | 1m / auto | L_high sys 1.72 [1.53, 1.92] (all) | L_high sys 1.61 [1.49, 1.72] + one hidden-queue excursion (2.38, other, special) (10/10) |
| hidden queueing | 2m | L_high sys 1.62 + excursion 2.08 (all) | L_high sys 1.70 [1.49, 1.90] + excursion 2.09 on the shifted grid (5/5) |
| missing instance (1 of 3 gauges; exact 0.667) | 1m / 2m / auto | L_low sys 0.664 / 0.665 / 0.664 (all) | L_low sys 0.661 [0.62, 0.70] / 0.663 / 0.661 (all) |
| overload ρ 1.5 × 60 s, arrivals counter | 1m | drain 0.277 special (exact 0.740: outside its interval [0.08, 0.47]); peak 1.57 promoted (exact 1.026) (11/14) | inconsistent_in_windows: drain 1.34 special and the peak promoted (backlog growth; R 1.04, exact ≈1.03), both on the shifted grid (15/15) |
| overload, arrivals | 2m / auto | drain 0.350 special (exact 0.787) (6/7) | drain 0.736 special (7/7) |
| overload ρ 1.5 × 60 s, completions counter | 1m | peak 2.35 special (exact 1.534), drain 0.231 (exact 0.637) (13/14) | peak 1.35 special, drain 0.559 special (15/15) |
| overload, completions | 2m / auto | drain 0.311 special (exact 0.728) (6/7) | peak 1.35 special + drain 0.574 special (7/7) |
| leak 2% stuck from t=120 s | 1m / auto | L_high sys 4.29 [2.13, 6.46], growing (all) | L_high sys 4.68 [2.54, 6.83], growing, slope > 0 (10/10) |
| leak | 2m | L_high, growing (6 windows: the range widened to wall-clock multiples) | L_high sys 4.67, growing NOT testable: 4 windows + a short one in 580 s (the trend over windows needs 6) (5/5) |
| any | 5m on 580-600 s | judged on 2-3 widened windows; overload/leak consistent or untestable | refused: fewer than two 5m windows in the range (hint stated) |

Every row at the default window (auto) matches the ground truth after the fix; the overload
spike at 5m (on the 900 s ranges) stays `consistent`: its whole episode (≈2 min) falls inside
one 5-minute window of both grids and balances there (exact R 0.98) — a resolution limit,
stated in `window_alignment`. In the seeded VM integration test
(tests/integration/test_queue_sim_vm.py) the spike is placed at three grid phases × 1m/auto ×
both counters, plus scrapes on the tile edges: with a completions counter it is special cause at
every placement; with an
arrivals counter one placement (2m) stays `consistent` with nothing promoted — there the exact R
of the episode's windows is within the intervals and close to 1 (the counter compensates) and
the backlog growth falls just under the distribution-free promotion threshold, so the check is
truthful but silent.
