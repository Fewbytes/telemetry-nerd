# Binding verdicts: per-signal health with evidence (bead czt.4)

For a USE / RED / Little's law binding: did each golden signal move against a stated reference,
how (a level change, a burst, a sustained episode), when did it start (with an interval), and
which one moved first. Every number is evidence (`core.wire.statistic`), every test is counted
in one family-wise error budget, and nothing averages percentiles.

## API

MCP `binding_verdict(source, kind+key | group | suggestion, range | start/end, step,
reference="auto"|"previous"|"day"|"week"|"profile", tz, matchers, error_matcher, alpha=0.05)`
and `TelemetryService.binding_verdict(...)`. Roles are resolved and planned exactly as
`show_binding` does (`BindingViews.resolve` + `plan_role`, czt.3), fetched through the same
`_fetch` (error ratio = errors / requests per member and step; latency = the histogram).
With `group=pgN` the group's window, step, matchers and error matcher are used and the group's
roles are annotated (`GroupRole.verdict`, `PanelGroup.verdict`): the UI shows a badge per role
and the first-mover line in the group header.

Returns `{binding, group?, range, step, reference, family, roles: {role: {status, direction,
pattern, method, level, episodes, onset, near_bound?, model_check?, evidence, caveats}},
summary: {moved, first, order, text}, caveats}`. `status`: `changed` | `no_change` |
`insufficient` | `gap` | `error`.

## Reference

k previous windows of the same length on the same step grid, each fetched like now (cached):
`previous` (4 adjacent windows before now), `day` (the same window on 7 previous days), `week`
(4 previous weeks); shifts by `analysis.seasonal.cycle_shifts` (local calendar days in `tz`,
23/25 h across DST). `auto` / `profile`: the cached operating profile of the role expressions
picks `week` (hour_of_week) or `day` (hour_of_day), else (auto only) `previous`; `profile`
without a seasonal profile is refused with a hint (`operating_profile` first). The choice and
its basis are stated. Fewer than 3 usable cycles: `insufficient` (as compare_seasonal).

Uncertainty comes from the spread across the reference cycles (random effects, Student t with
k-1 df), never below each cycle's own sampling noise: one quiet hour is not "normal".

## Per-role methods (analysis/verdicts.py, pure)

Two detectors per role, each on now against the reference cycles:

1. **Level** (whole window). A per-cycle level and its sampling variance; now against a
   Student-t prediction interval from the kept reference levels (atypical cycles excluded by
   `seasonal.atypical_levels`), s^2 = max(sample variance, mean sampling variance),
   var = s^2 (1 + 1/k) + max(0, V_now - mean V). p two-sided.
2. **Episode** (within the window, so onsets can be timed). A standardised per-step residual
   relative to now's own robust level, two-sided tabular CUSUM (k = 0.5) run without restarts:
   an episode is an excursion that exceeds h. h is solved so that the in-control probability of
   any episode over the window's N blocks is at most the detector's alpha, from the Brook-Evans
   ARL (`spc.cusum_arl`, h <= 30) under a conservative scenario as in `analyze` (sigma at its
   one-sided 95% upper bound from the reference's points, now's median centre off by one
   standard error). Maximalist spread: when now's own robust spread (from successive
   differences, so a shift or an episode does not inflate it) is wider than the reference's,
   now's is used (`noisier_than_reference`: e.g. a share's variance at a higher share).
   Autocorrelation:
   when the reference residuals' lag-1 phi is significant the CUSUM runs on AR(1) residuals
   (as SPC). Heavy-tail guard: the same detector on every reference cycle; if any would fire, h
   is raised above its largest excursion (`heavy_tails`).

| role | per-step data | level | episode residual |
|---|---|---|---|
| RED errors (error ratio) | errors a_t, requests n_t = rate x step (summed over members) | logit of the window share on **effective n** = N / (phi tau): phi the Pearson dispersion of per-step counts (reference median, >= 1), tau the integrated autocorrelation of the Pearson residuals | Pearson (a - n p_c) / sqrt(phi n p_c (1 - p_c)), steps merged into blocks with >= 5 expected errors |
| duration / latency (histogram) | requests above a threshold x and all requests, per step, from the histogram | as errors (the share above x) | as errors |
| rate / arrival_rate, concurrency, saturation, utilization | the per-step value (log scale when all > 0, else linear) | mean of the window | (value - day/week phase shape - now's median) / within-cycle robust sigma of the reference |
| USE errors (a rate of events) | events a_t = rate x step | log rate, quasi-Poisson | Pearson for Poisson, blocks >= 5 expected |

- **Latency threshold** from the reference only (never from now): the bucket edge shared by
  all cycles whose pooled share above is nearest 5% (the reference's ~p95 edge) with >= 20
  expected above per cycle; exact at a bucket edge (fraction_over). The share above it is a
  binomial share per step; percentiles are never averaged. The shape distance (largest CDF
  difference, now vs the pooled reference) is reported, descriptive.
- **Members.** Additive roles (rates, error and latency counts, concurrency) are judged on the
  total over members. Utilization and saturation are judged per member (not additive; one hot
  member matters), Bonferroni across members inside the role's budget; the role reports the
  worst member.
- **Near bound** (utilization): runs of >= 3 steps at >= 90% of the natural bound (1, or 100
  for percent, from the data and the expression). Exact counts, a stated threshold rule, not a
  test: it qualifies a change (`at_capacity`) and does not spend alpha. A saturation metric whose
  name says throttling is noted as such. (A saturation `bounded_by` limit line is a follow-up.)
- **Concurrency (Little's law binding).** Judged as a value, plus `model_check`: the
  check_littles_law verdict over the same window (czt.2; L vs lambda W with its own propagated
  interval and its own stated 5% FWER, reported, not counted in this family).

**Pattern.** `level` (the window differs, no onset inside it), `shift` (a single changepoint
inside the window), `blip` (an episode of <= 2 blocks that ended), `burst` (an episode that
ended: >= 3 blocks follow its CUSUM peak), `sustained` (an episode still open at the end).

## Onsets and ordering

Onset = Page's estimator: the first step of the CUSUM excursion that fired (in the direction of
the level change when the level detector fired too). Interval = [onset - one block - the rate
window's lookback, the step at which it fired]: a change cannot be detected before it happens,
and rate() smears a change back by its window. When the level detector fired but no episode in
its direction exists (the change covers most of the window), the single changepoint of the
CUSUM-of-deviations test (`stability.changepoints`) is used, with Bai's (1997) 95% interval
+- 11.03 (sigma_lr / delta)^2 steps; failing that, "before the window".

Ordering is only claimed when intervals do not overlap: "errors moved first (onset 12:03Z,
11:58-12:05Z); duration followed (12:20Z)"; overlapping intervals read "simultaneous within
+-X" with X the half-width of their union. Never more precise than the intervals.

## Multiple comparisons

One family per call: FWER alpha (default 5%) over every judged role, Bonferroni:
alpha_role = alpha / m (m = roles with data), split equally between the level and the episode
detector (alpha / 2m each), and across members for per-member roles. Stated in `family`. A quiet
binding therefore raises no flag with probability >= 1 - alpha (calibration below).

## Evidence and uncertainty

Per role `evidence`: the level statistic (odds ratio of the share or ratio / difference of the
level vs the reference, with its 95% prediction interval), now's share with a Wilson interval
on effective n, and `onset_ms` with its interval for a changed role, citing the role's dataset
(error ratio: the ratio dataset; parameters name num / den). `core.uncertainty.mark_statistics`
runs over the datasets the statistics were computed from (num / den, histogram, value
datasets): clean source data leaves them unflagged; anything else flags them (spec §5.3).

## Validation (seeded, tests/unit/test_verdicts.py; `scripts/calibrate_verdicts.py`)

Synthetic RED and USE sets (1-min steps, 1 h windows, 4 previous windows; requests ~3000/step
with per-cycle level jitter and AR(1) noise; error share 0.2% with overdispersion; lognormal
latency in classic buckets with a jittered median; utilization ~0.4 AR(1)):

- quiet: no role flagged at the family alpha (numbers in the calibration table below);
- error burst (x10 for 8 min): errors `changed` (burst), onset interval covering the truth;
- latency shift (median x1.6 from minute 20) then an error burst at minute 40: duration first,
  errors second, ordering claimed;
- saturation episode (utilization -> 0.97 from minute 20 to 40, queue x5 from 22): utilization
  and saturation `changed`, utilization `at_capacity`, simultaneous (2 minutes apart, onset
  intervals overlap).

Service tests (`ScenarioSource`: values keyed by minute and member, so every reference window
is reproducible) run the same scenarios end to end through `binding_verdict`, the group
annotation and the MCP tool.

Calibration (`uv run scripts/calibrate_verdicts.py`, 400 seeds each):

| scenario | any role flagged | planted role(s) flagged | onset error (min) | onset interval covers | order right / wrong / simultaneous |
|---|---|---|---|---|---|
| quiet RED | 0.5% | - | - | - | - |
| quiet USE | 1.2% | - | - | - | - |
| error burst x10, min 30-37 | 100.0% | errors 100.0% | 0.0 | 100.0% | - |
| latency median x1.6 from min 20 | 100.0% | duration 100.0% | 0.0 | 92.0% | - |
| error burst x3, min 30-37 | 97.0% | errors 97.0% | 0.0 | 100.0% | - |
| latency median x1.2 from min 20 | 97.8% | duration 97.5% | 0.0 | 90.3% | - |
| request rate x0.6 from min 30 | 100.0% | rate 100.0% | 0.0 | 93.5% | - |
| latency x1.6 from 20, then errors x10 at 40-47 | 100.0% | duration 100.0%, errors 100.0% | 0.0 | 97.0% | 400 / 0 / 0 |
| saturation: util 0.97 min 20-40, queue x5 from 22 | 100.0% | utilization 100.0%, saturation 100.0% | 0.0 | 99.1% | 8 / 0 / 392 |

Quiet, 2000 seeds: any role flagged 2.0% (RED) / 1.8% (USE) at the nominal family-wise 5%
(conservative; per role 0.3-1.1%). Onset intervals cover the truth 90-100% (Bai's interval
for a shift covering most of the window runs slightly below its nominal 95%). Power is what k
reference windows allow at alpha / 2m: a 20% latency median shift is caught ~98% of the time,
a 3x error burst of 8 minutes ~97%; the episode detector carries most of it (the level
detector's t with k-1 = 3 df at 0.8% is strict).
