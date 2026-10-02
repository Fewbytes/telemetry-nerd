# Seasonal comparison: `compare_seasonal` (bead lkn.2)

"Is now unusual for this time of day / week?" Compare the current window with the same phase of
previous cycles, with uncertainty from the spread **across** those cycles (never one noisy week).
Builds on reference windows (4ok.6), the operating profile's leave-one-out band (2as.7),
autocorrelation / n_eff (lkn.1) and the red-noise spectrum (lkn.4).

## Principles

- The reference is chosen from the data and **stated**: which cycles, how many, how they are
  aligned (UTC or a named timezone), which were excluded and why.
- Uncertainty comes from what previous cycles actually did at the same phase (leave-one-cycle-out
  residuals), not from within-window noise and not from one reference cycle.
- Percentiles are never aggregated across cycles: for a percentile series with a known histogram,
  or a distribution dataset, the histogram is compared per cycle (see Latency, lkn.7); summary
  quantiles are refused with a hint. Raw counters are refused as for other time ops.
- Gaps are never interpolated: a phase is compared only where now and >= 3 cycles have data.
- Fewer than 3 usable cycles -> `insufficient_history`, saying what was found.

## References (schemes)

The dataset is the current window W = [start, end] at step s (N points). Candidate schemes:

| scheme | cycles | shift of cycle j |
|---|---|---|
| `previous` | 4 | j x span (adjacent windows; the non-seasonal baseline) |
| `1d` | 7 | j local calendar days |
| `1w` | 4 | j x 7 local calendar days |

A scheme is offered only when the span fits in its period (cycles must not overlap now). Each cycle
is fetched as its own dataset (same expr, step, source; cached) and put on now's grid.

**Alignment / DST.** With `tz` (IANA name; default `UTC`), every point is matched by local wall
clock (lkn.8): point i at UTC t_i is compared with the instant whose local time is
`local(t_i) - j days`. Across a DST change the shift is 23 h or 25 h, so "same local hour
yesterday" stays the same local hour, and this holds point by point when the window itself (or a
previous cycle) crosses a change. A local time that did not exist on the cycle's day (spring
forward) leaves that point uncompared; a local hour that occurs twice in the window (autumn) is
compared twice with the same reference hour; either case sets caveat `dst_wall_clock` (it replaces
lkn.2's `dst_within_window`, which flagged windows aligned at their start only). Every shift must
be a multiple of the step (else refused with a hint). Cycles with any point not exactly j nominal
periods back are listed (`dst_shifted`). UTC alignment is stated as such: human-driven load
follows local time (see 2as.24).

**Choice.** Each available scheme is scored by leave-one-cycle-out mean absolute error: each cycle
predicted pointwise by the median of the others. Preference order previous -> 1d -> 1w; a later
(more specific) scheme must cut the error by >= 5% to be chosen (`scores`). Choosing `1d`
or `1w` is the seasonality evidence for this window; `previous` means no daily/weekly cycle helps
and the answer says so. A cached operating profile's seasonal model (2as.7) is reported alongside
(`profile_period`) but does not decide.

## Exclusions

- `missing`: a cycle with data at < 50% of the grid.
- `user`: cycles overlapping `exclude` (local dates, e.g. holidays).
- `atypical`: cycles whose level (mean deviation from the median of all cycles) does not fit the
  others, by forward search from a clean subset (lkn.8): start from the k-2 cycles whose levels
  vary least (k-1 for k <= 5), then repeatedly test the outsider nearest the subset mean against
  the subset's Student-t prediction interval (sigma never below the SE of a cycle mean from point
  noise) and add it while it fits; at the first misfit every remaining cycle is atypical. The
  per-step level is calibrated by seeded simulation (1e6 null sets per k) so that a set of normal
  cycles loses one with probability 1% (`FORWARD_P`). A clean start means a second atypical cycle
  (Saturday next to Sunday among daily references) cannot inflate the spread it is judged by; the
  lkn.2 rule (each cycle against all others, Bonferroni) never excluded both. A holiday or
  incident in the reference must not widen the band or bias the centre. The scheme's choice
  score is computed before this exclusion (weekends among daily references count against `1d`).
Every exclusion is listed with its reason and caveat `cycles_excluded`.

## Statistics (per series)

Values are compared on a log scale (ratio) when all values are > 0, else linearly (difference);
`scale` says which.

- **Centre** c(i): median over kept cycles at phase i (>= 3 observed; else no comparison there).
- **LOO residuals** r_j(i) = x_j(i) - median of the other cycles at i. Their spread is the
  cycle-to-cycle variation at a fixed phase plus noise. They are pooled over phases in contiguous
  phase blocks (each >= 100 residuals, at most 24 blocks; one pool if too few): local
  heteroscedasticity, like the profile's per-hour-of-day pools. LOO predicts from k-1 cycles, the
  centre uses k: residuals are rescaled by sqrt((1 + v_k) / (1 + v_{k-1})), v_n = variance of a
  normal median of n relative to sigma^2 (exact for n <= 7, pi/2n beyond).
- **Band** (drawn): c(i) + Weibull (type 6) 5% / 95% quantiles of the block pool: central 90%
  prediction band for a normal cycle.
- **Ratio view**: d(i) = x_now(i) - c(i) (log ratio or difference), with the band in the same units.

## Verdict

Three detectors, each at alpha = 1% for the window:

1. **level** (whole window): D_0 = mean d(i) vs the cycle levels D_j = mean (x_j - c). Prediction
   interval D-bar +- t_{k-1} s_D sqrt(1 + v_k): Student t with k-1 df because only k cycles inform
   the spread; v_k because the centre is a median of k. Reported as ratio (or difference) now / reference with its 90% interval; flagged when
   outside the 99% interval.
2. **extremes** (short anomalies): z(i) = (d(i) - median d) / sigma_block (1.4826 MAD of the
   block pool of residuals, each cycle relative to its own median). Threshold: Sidak over the
   N compared points, z* = t_df^-1(1 - alpha / 2N), df = 0.2 x block pool / tau (see Calibration). Guard for heavy tails: the same rule is applied to every
   previous cycle; if any would have fired, z* is raised to the largest previous excursion and
   caveat `heavy_tails` is set.
3. **outside band** (sustained sub-window deviations): share of points outside the 90% shape band
   (same level-relative residuals), binomial test on N_eff = N / tau (p < 1%), and larger than
   every previous cycle's share.

`verdict`: `insufficient_history` | `unusual` (any detector) | `usual`, with `direction`
(higher / lower / mixed), `reasons` with numbers, and `evidence` statistics (seasonal ratio with
interval, band exceedance share).

## API

- MCP `compare_seasonal(dataset, cycles=["previous","1d","1w"], tz="UTC", exclude=[])` ->
  `{dataset, window, alignment, schemes, profile_period, series: [{labels, verdict, direction,
  reasons, reference: {scheme, label, cycles, excluded, scores, dst_shifted?}, scale, n, n_eff,
  ratio | difference (value, normal_90, interval_99, previous_cycles, evidence), outside_band,
  extremes}], caveats, draw}`. The scheme is chosen per series.
- `show(dataset, question, mark="seasonal")` after `compare_seasonal`: per series, now (bold),
  previous cycles (faint), centre (dashed) and 90% band; flagged points; a ratio view toggle.
- `analyze` and `spectrum` add `suggest: compare_seasonal(...)` when a significant period
  matches 1d or 1w; `show` (lines) does when the cached operating profile is seasonal.

## Calibration (seeded simulation, 1000 seeds per row)

`uv run python scripts/calibrate_seasonal.py`. Weekly load (weekday business-hours peak, low
weekends), multiplicative noise (white or AR(0.7) within the window), optional per-cycle level
jitter (sd 5%), 96-point windows, 1w scheme (lkn.8 numbers; lkn.2's rule gave the same within
0.6 points, atypical exclusion of a normal cycle at k=6 with jitter 0.8% then, 0.2% now):

| k | noise | jitter | band coverage (90%) | level 90% PI coverage | false alarms: level / extremes / outside / any |
|---|---|---|---|---|---|
| 3 | white | 0 | 0.902 | 0.907 | 1.4% / 0.1% / 1.4% / 2.9% |
| 3 | AR(0.7) | 5% | 0.890 | 0.912 | 0.8% / 0.0% / 0.5% / 1.3% |
| 4 | white | 0 | 0.902 | 0.916 | 0.9% / 0.1% / 1.2% / 2.2% |
| 4 | white | 5% | 0.891 | 0.888 | 1.7% / 0.1% / 0.6% / 2.4% |
| 4 | AR(0.7) | 5% | 0.883 | 0.887 | 1.0% / 0.0% / 0.3% / 1.3% |
| 6 | white | 5% | 0.895 | 0.895 | 1.1% / 0.5% / 0.7% / 2.3% |
| 6 | AR(0.7) | 0 | 0.898 | 0.894 | 1.0% / 0.0% / 0.0% / 1.0% |

Atypical cycles (lkn.8), weekday 09-17 window; "all atypical" = every planted atypical cycle
excluded, never a normal one in any row:

| scenario | lkn.2 rule | forward search |
|---|---|---|
| 1d k=7, Sat+Sun in refs, jitter 5% (weekend level -0.44 = 9 sd) | 0.0% | 27% |
| 1d k=7, Sat+Sun in refs, jitter 2% | 0.0% | 90% |
| 1w k=5, one holiday week x0.6, jitter 5% | 52% | 48% |

Power is bounded by k: the spread of k-2 levels is a t with k-3 df. On iid normal levels with
two planted at -delta sd, k=7 excludes both 58% / 94% of the time at delta 10 / 15 (k=8: 81% /
100%); with one planted at 10 sd, 86% (lkn.2 rule 92%). k <= 5 tests only the single most
outlying cycle (as lkn.2 did): two atypical among five are out of reach at 1%.

Choices that calibration forced (each was wrong first):
- Detectors 2/3 judge each window relative to its own median level: per-point tests on the raw
  residuals counted the cycle-level jitter once per point (outside-band alarms on normal days).
- The level interval uses in-sample cycle levels and `1 + v_k` (a median of k), not LOO means:
  LOO cycle means are correlated and gave the wrong scale.
- Atypical cycles: a Hampel rule with k = 4..7 excluded a normal cycle > 10% of the time (MAD of
  a handful of values), which shrank the band; replaced by the t prediction interval (~1%).
  That rule was masked by a second atypical cycle; the forward search (lkn.8) is calibrated per
  k instead of Bonferroni (its start is the least-varying subset, whose spread is biased low).
- Extremes: sigma is a MAD from a block pool; Student t with df = 0.2 x pool / tau (MAD is 37%
  efficient and LOO residuals sharing a phase are dependent), calibrated: <= 0.6% at nominal 1%.

## Validation (seeded, in tests)

- Daily + weekly synthetic series, multiplicative noise and per-cycle level jitter: a weekday-like
  peak on a Saturday is `unusual`; the normal Monday peak is `usual`; the 1w scheme is chosen.
- Band coverage: over 40 seeds, held-out normal cycles fall inside the 90% band at the nominal rate
  (bounds asserted in tests; numbers above).
- False alarms: normal cycles flagged `unusual` at a rate consistent with the 3 x 1% design.
- Alignment: shifts across the EU spring / autumn DST changes are 23 h / 25 h with `tz`, 24 h in UTC;
  a window across the change is matched point by point (the local 10:00 peak stays aligned).
- Insufficient history, missing cycles, user and atypical exclusions, percentile refusal.

## Latency: the histogram per cycle (lkn.7)

`compare_seasonal` on a `histogram_quantile` series whose histogram is known, or on a
`query_distribution` dataset, never compares percentiles: it fetches the histogram for now and
for each previous cycle (same schemes and cycle counts; the cycle's window is the same local
wall-clock window) and works on each cycle's **window histogram** (bucket counts summed over the
window; counts are additive, percentiles are not, so nothing is pooled or averaged across cycles
except counts where a pooled CDF is the reference for the descriptive shape distance).

- **Threshold.** The share above x per cycle is exact at a bucket edge (fraction_over). A
  requested x snaps to the nearest edge shared by every cycle (log distance) and says so. The
  default is chosen from the reference cycles only (never from now): the edge whose pooled share
  above is nearest 1% among edges with >= 20 expected observations above per cycle, else the
  nearest-1% edge with caveat `few_over_threshold`.
- **Band.** Per cycle the empirical logit L_j = logit((a_j + 1/2) / (n_j + 1)) with binomial
  variance V_j. The share band is a Student t prediction interval with k-1 df around the mean of
  the kept cycles' L_j with variance s^2 (1 + 1/k) + max(0, V_now - mean V), s^2 = max(sample
  variance, mean V) (random-effects: cycle-to-cycle variation, never below sampling noise; now's
  extra sampling noise when it has fewer observations). Requests within a window are not
  independent, so binomial noise alone would be too tight; the across-cycle spread carries it.
- **Verdict.** unusual when now's share lies outside the 99% interval (alpha 1%), with direction;
  `share_over` reports value, normal_90, interval_99, centre and an `evidence` statistic.
- **Atypical / missing / user exclusions** as for time series (forward search on L_j, floor =
  binomial SE; missing = no observations or < 50% of the steps with data).
- **Choice** of the reference by LOO error of L_j, same rule as above.
- **Shape (descriptive).** Largest |CDF difference| at the shared edges, now vs the kept cycles'
  summed counts, and each cycle vs the others. Not a detector: with k cycles now exceeds all of
  them by chance 1/(k+1) of the time.
- Each cycle lists its n, share and dataset (draw one with `show(mark="histogram")`); there is
  no seasonal panel for histograms yet.

Calibration (`scripts/calibrate_seasonal.py --only lkn7`, 1000 seeds; lognormal latency, median
80 ms, sigma 0.6, per-step log-median jitter 0.1, per-cycle jitter as listed, classic le buckets,
default threshold 250 ms = ~3% above):

| k | n/window | cycle jitter | share 90% PI coverage | false alarms (1%) | detect 2% x5 slower | detect 5% x5 slower |
|---|---|---|---|---|---|---|
| 3 | 2000 | 5% | 0.977 | 0.0% | 0% | 0% |
| 4 | 2000 | 5% | 0.921 | 0.0% | 0% | 12% |
| 4 | 50000 | 5% | 0.908 | 0.5% | 6% | 28% |
| 4 | 50000 | 0 | 0.911 | 0.1% | 62% | 100% |
| 7 | 2000 | 5% | 0.906 | 0.8% | 7% | 44% |
| 7 | 50000 | 10% | 0.891 | 1.3% | 3% | 10% |
| 4 | 300 | 5% | 0.959 | 0.0% | 0% | 0% |

Conservative at k = 3 and small n (the sampling-noise floor binds when s^2 has 2 df). Power is
what the cycles allow: a 5% jitter of the median moves the share above 250 ms by ~20% from day to
day, so a tail change must exceed that spread; with stable cycles it is caught at once.

## Out of scope (follow-ups)

- A seasonal panel for histograms (per-cycle survival curves with the share band).
- Seasonal centre for SPC / prior-cycle baselines in `analyze` (lkn.5).
- Learning the timezone per source (2as.24).
