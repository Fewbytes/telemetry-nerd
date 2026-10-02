# Fleet analysis: `fleet` (bead lkn.3)

"Show me the CPU of all nodes": many series of one metric are analysed as a GROUP (a fleet) instead
of being drawn as 100 lines. Spread per step, outlying members with evidence, churn and missing
data. Builds on robust centre/scale (stability), autocorrelation / n_eff (autocorr, lkn.1), the
series-budget rule (MVP spec §6.3: > 12 series -> group band + outliers) and the missing-data
semantics of the series-bundles spec (§5.2, §7.1: alive / reporting / silent).

## Principles

- **Never aggregate percentiles across members.** Percentile datasets (representation `quantile`,
  `histogram_quantile` / `quantile_over_time` expressions, summary series with a `quantile` label)
  are refused with a hint: compare per-member *rates* or *threshold fractions* (fraction_over), or
  the histograms. The median of member p99s is not the fleet p99.
- The fleet at a step is a **population, not a sample**: per-step quantiles across members are
  descriptive (no interval), computed only over members that reported at that step. Missing members
  reduce n; nothing is imputed, interpolated or carried forward. n per step is returned and drawn.
- Outliers are judged **relative to the fleet**, with robust statistics that the outliers cannot
  move (median / MAD, leave-one-out), and with the multiple comparisons across members x tests x
  steps controlled (family-wise 1%).
- Units must agree across members (one metric, or names whose catalog units agree); else refused.
- Distributions and raw counters are refused as for every time op.

## Members and grid

A member is one series of the dataset. `by` (optional label names) names members and must
identify them uniquely; when several series share the `by` values the call is refused with a
hint (`sum by (pod) (...)` / `avg by` at the source: which one is a semantic choice). Without `by`
the member name is the labels that vary across the fleet. At least 5 members (else: draw lines /
small multiples); outlier tests need >= 10 members with enough data. Ranges longer than 1440 steps
are averaged per member to a coarser step first (caveat `coarsened`; per-member means of a rate or
gauge are honest, unlike means of percentiles). The source caps a query at 500 series; analysis
cost is O(members x steps), the panel draws aggregates plus at most 6 members.

## Spread (per step, raw units)

Over the members reporting at the step (n_t): median, quartiles (n_t >= 5), 10/90% (n_t >= 10),
min/max envelope. Quantiles: linear interpolation between order statistics (Hyndman-Fan 7).
Quantiles commute with monotone transforms, so the band is the same on a log or linear axis.
`alive_t` = members seen at or before t (a member that stops reporting stays alive: silent);
coverage `n_t / alive_t`, and `missing_share` = 1 - sum n_t / sum alive_t.

Summary: median relative spread (IQR / median on log scale as q75/q25), spread in the last vs the
first third, time of the widest spread, min/median n per step.

## Outliers

**Scale.** Deviations are computed on log values when every value is > 0 (multiplicative noise and
members of different size: a ratio is the honest comparison), else linear; `scale` overrides.
**Normalisation** (`normalise`): `none` (default; members compared as they are, so a member that
is consistently higher is a persistent outlier) or `member` (each member divided by / minus its own
median over the window: only *shape* is compared, level differences between heterogeneous members
such as different instance sizes are removed, and the level test is off). The choice is stated.

**Per-step deviation.** For member i at step t: d_it = y_it - median of the OTHER members at t,
z_it = d_it / s_t. s_t is a robust sigma (1.4826 MAD x the Croux-Rousseeuw small-sample factor;
1.2533 mean absolute deviation if the MAD is 0) of the other members' deviations from their
per-step median, **pooled over +-6 steps** (local heteroscedasticity, like seasonal's block pools):
one step's 30 members would give the MAD so few degrees of freedom that the per-step threshold
balloons (t with df ~ 10 at 1e-7). Leave-one-out is exact for <= 64 members; beyond, the
full-fleet median/MAD (one member moves them by O(1/n), stated as `leave_one_out: false`).
Subtracting the per-step median removes everything the fleet shares (load, diurnal cycle).

**Member statistics** (members with >= 10 z values and >= half of their alive steps):

| test | statistic | catches |
|---|---|---|
| level | L_i = 20% trimmed mean over t of z_it | consistently off (persistent) |
| change | C_i = trimmed mean of z in the last third - in the first third | drifting / shifted |
| excursions | max over t of abs z_it (spike), of its 5-step and 15-step rolling medians (episodes) | transient |

Trimmed means make level blind to an episode under 20% of the window and change to one under 20%
of a third. **Not medians:** the time-median of median-centred deviations is miscalibrated (2-4%
false alarms against a 0.33% design in simulation): every step's median forces exactly half the
members above it, which distorts the across-member distribution of time-medians. The trimmed mean
is calibrated (0.2-0.3%).

**Across-member tests (level, change).** Each statistic is compared with the other members' values
by a leave-one-out robust z: (S_i - median_others) / (1.4826 MAD_others x small-sample factor).
The null distribution is the fleet itself: how much members' window means naturally differ,
including their autocorrelation (an autocorrelated member's L_i varies more, and so do everyone
else's), so no tau model is needed here. Threshold: Student t with df = 0.3675 (K - 1) (the MAD's
37% efficiency: var(sigma_MAD) = 1.3605 sigma^2 / n), Bonferroni over K members; each test at
alpha / 3. Checked on iid normal vectors: realised family-wise rate 0.15-0.32% for K = 10..300
against 0.33%. Student t quantiles are computed exactly (incomplete beta), not by series
expansion: the far tail matters at Bonferroni levels.

**Excursion tests.** Three durations share alpha / 3: single steps, and rolling medians over 5
and 15 steps (most of the window must be out, so isolated heavy-tailed noise cannot fake an
episode). Per member-step threshold: Student t with the pooled sigma's df (0.3675 x pooled values
/ min(tau, 13) - 1, tau = the fleet's typical autocorrelation time of deviations), Bonferroni over
all member-steps tested; for the rolling medians, in units of their own robust scale.

**Episodes are scanned on prewhitened deviations** (lkn.14): the rolling medians run over the
AR(1) innovations r_t = (z_t - phi z_(t-1)) / sqrt(1 - phi^2), phi = median over the calm members
of their lag-1 autocorrelation (stated as `episode_scan`). Under heavy-tailed noise one huge
innovation decays over several steps (phi^j) and can hold a rolling median of raw z up: with t(3)
innovations that was 3.7% family-wise from the 15-step scale alone, while that scale's own tail
check (at 0.1% / 0.01%) passed. Innovations do not carry over, so a median of w of them is
exceeded only when ceil(w / 2 tau_r) roughly independent values are, and its tail index is that
multiple of a single step's. Power is unchanged where it matters: a sustained shift delta becomes
(1 - phi) delta against innovation noise sqrt(1 - phi^2) sigma; for the 15-step median at
phi = 0.6 that is the same signal-to-noise as raw z (the median of 15 iid values is tighter than
of 15 autocorrelated ones by just as much). At phi = 0.9 the episode scales lose power, but there
the single-step test finds a sustained shift anyway (calibration: transient detection 99.7%).

**Heavy tails.** Real fleets are often spiky. Check: does the typical member (20% trimmed mean
over members of the exceedance share, so a few faulty members cannot trip it) exceed t_df's
two-sided 0.1% or 0.01% points more often than t says (Poisson test, 0.1%)? If so at a scale, that
scale's threshold also must beat a Gumbel fitted (from quartiles, leave-one-out) to the OTHER
members' log peaks at p = alpha / 3 / 3 / K: the log of a maximum is Gumbel-like for normal and
Frechet tails alike, and the fleet's own peaks are the empirical null. A rolling median that needs
fewer than 5 independent exceedances (ceil(w / 2 tau_r) < 5: the 5-step scale, 3) inherits the
single-step verdict; the 15-step scale (8) is judged by its own check. Caveat `heavy_tailed_noise`.
Hill-tail extrapolation was tried first and rejected: extrapolating five decades from the top
0.5% was unstable (thresholds 40-200).

**The Gumbel bar carries its own fit noise** (lkn.14). Extrapolating from K - 1 peaks to
p ~ 1e-5 is noisy: by the delta method with the asymptotic covariances of the quartiles, the
quantile at reduced variate g has variance V(g) beta^2 / n (about 2.1 beta^2 at n = 100,
g = 11.5: sd ~ 1.5 beta). A bar that is right on average is exceeded MORE often on average
(the tail exp(-x / beta) is convex: E[p e^(-eps)] = p e^(s^2 / 2), here ~ 3p), so the bar is raised
by beta V / 2n; the false-alarm rate averaged over the fit's noise is then p. This replaced a
fixed inflation factor (1.0; 1.15 calibrated just as well but is a knob, this is derived). Under
t(3) the honest single-step bars are 60-250 robust sigmas: at Bonferroni levels such fleets'
spikes carry little evidence, and a sustained excursion is found by the 15-step scale.

**Family-wise error.** Under a homogeneous fleet the chance of naming any member is <= ~1% by
construction (three tests at 1/3 each, Bonferroni within each), and about that under heavy-tailed
noise down to t(3) (1.3% at 300 seeds, 1.6% at 1000). See Calibration.

**Classification and since-when.** Each flagged member has `kind`:
- `drifting` (change fired, gradual: a linear trend fits its deviation better than one step) or
  `shifted` (change fired, a step fits better; `at` = the best split),
- `persistent` (level fired, change did not),
- `transient` (only excursions fired): episodes = runs of steps beyond the threshold, merged
  across gaps shorter than the member's autocorrelation time tau; an episode is `sustained` when
  longer than tau steps (more than one noise excursion can explain), else `momentary`.
`since`: start of the final stretch where the member's rolling median z (window max(5, 2 tau))
stays beyond 2 in the outlier's direction; `since_window_start` when it covers the window start
(the member was off at least since then). `score` = the largest |statistic| / threshold (>= 1).
The member's tau is computed on its calm steps with a linear trend removed (a drift is signal,
not noise) and never below the fleet's typical tau.

**Effect sizes and evidence.** Offset: 20% trimmed mean of d over the window, as a ratio to the
other members (log scale) or a difference, with a 99% interval: winsorized sd / ((1 - 2 x 0.2)
sqrt(n_eff)), t with n_eff - 1 df, n_eff = n / tau (Tukey-McLaughlin with autocorrelation).
Change: last third minus first third, intervals combined. Transient: the same over the strongest
episode. Each carries an `evidence` statistic (`fleet_member_offset` | `fleet_member_change` |
`fleet_member_excursion`) for finding_create.

**Heterogeneous fleets.** When > 10% of the tested members are flagged, caveat `many_outliers`:
members differ systematically (sizes, roles, zones). Hint: `normalise="member"` to compare shapes,
or query per sub-group (a label) and analyse each. Clustering into k behaviour groups is a
follow-up.

## Churn and missing data (series-bundles semantics)

- `appeared`: first seen after the window start (+ tolerance max(3 steps, 5%)).
- `stopped_reporting`: last seen before the end (same tolerance), with `since`. The source cannot
  tell a series that ended (pod replaced) from one that went silent (the sick one), so both are
  listed next to the outliers, never silently dropped (spec §5.2: silent members join the outlier
  set). Appeared ~ stopped suggests replacement (stated).
- `missing_share` and min n per step; caveat `members_missing` when > 5% (not `missing_data`: that code is the located
  bucket_state caveat), `members_skipped` when some
  members have too little data to be tested (listed by count).
- Source-aggregated expressions (`sum(...)`) are one series: refused (< 5 members).

## API

- MCP `fleet(dataset, by=None, scale="auto", normalise="none")` ->
  `{dataset, effective_step, members: {count, tested, untested, named_by, never_reported},
  scale, normalise, spread: {...}, coverage: {n_per_step, alive_max, missing_share},
  churn: {appeared, stopped_reporting, note?}, outliers: [{member, labels, kind, direction, score,
  tests, z, since, since_window_start, offset, change?, at?, episodes?, evidence}], tests: {
  family_wise_alpha, leave_one_out, member_threshold_z, excursion_thresholds_z, ...}, caveats,
  draw}`. At most 10 outliers listed (by score; `more_outliers` counts the rest).
- `show(dataset, question, mark="fleet")` (uses the latest `fleet` options, else defaults): band
  (min-max, 10-90, 25-75 as nested grey fills: spread is intensity, no boundary lines), median
  line, outlier members drawn and labelled (<= 6, Okabe-Ito), a coverage strip at steps where
  alive members did not report (darker = more missing), the legend "100 members · 3 outliers ·
  92-100 reporting per step · shading ..." and one line per drawn outlier (kind, direction,
  since).
- The chart validator's `series_budget` refusal for line charts now suggests `fleet`.

## Calibration (seeded simulation)

`uv run python scripts/calibrate_fleet.py --seeds 300` (fleets from `tests/unit/fleet_sim.py`):
288 steps, shared daily load x exp(member level (sd 5%) + AR(1) noise (sd 10%)); planted members:
persistent x1.8, transient x2.7 for 12 steps mid-window, drifting 0 -> x2.2 over the window.
False alarm = share of homogeneous fleets in which ANY member is named (design: 1%). Detection =
the planted member named with the right kind (drifting may be `shifted`).

| scenario | false alarm (any member) | by test level/change/spike/episode/long | detect persistent/transient/drifting |
|---|---|---|---|
| M=10 AR(0.6) normal | 0.3% | 0.3% / 0.0% / 0.0% / 0.0% / 0.0% | - |
| M=30 AR(0.6) normal | 0.3% | 0.0% / 0.0% / 0.0% / 0.3% / 0.0% | 99.7% / 100.0% / 100.0% |
| M=100 AR(0.6) normal | 0.3% | 0.0% / 0.3% / 0.0% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% |
| M=300 AR(0.6) normal | 0.0% | 0.0% / 0.0% / 0.0% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% |
| M=100 white normal | 2.0% | 0.0% / 0.7% / 0.3% / 0.7% / 0.3% | 100.0% / 100.0% / 100.0% |
| M=100 AR(0.9) normal | 0.7% | 0.3% / 0.3% / 0.0% / 0.0% / 0.0% | 100.0% / 99.7% / 100.0% |
| M=100 AR(0.6) t(4) | 1.0% | 0.0% / 0.3% / 0.0% / 0.0% / 0.7% | 100.0% / 100.0% / 100.0% |
| M=30 AR(0.6) t(4) | 0.7% | 0.0% / 0.7% / 0.0% / 0.0% / 0.0% | 99.3% / 100.0% / 100.0% |
| M=100 AR(0.6) 10% missing | 0.3% | 0.0% / 0.0% / 0.0% / 0.3% / 0.0% | 100.0% / 100.0% / 100.0% |
| M=100 no heterogeneity | 1.3% | 1.0% / 0.3% / 0.0% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% |
| M=100 AR(0.6) t(3) | 1.3% | 0.0% / 0.7% / 0.7% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% |

Before lkn.14 (raw-z episode scan, Gumbel bar without its fit noise), the heavy-tailed rows were:

| scenario | false alarm (any member) | by test level/change/spike/episode/long |
|---|---|---|
| M=100 AR(0.6) t(4) | 1.3% | 0.0% / 0.3% / 0.0% / 0.0% / 1.0% |
| M=30 AR(0.6) t(4) | 1.3% | 0.0% / 0.7% / 0.7% / 0.0% / 0.0% |
| M=100 AR(0.6) t(3) | 5.3% | 0.0% / 0.7% / 1.0% / 1.3% / 3.7% |

Reading: at or under the 1% design for normal noise with autocorrelation, missing data, any fleet
size; 2.0% for white noise (6 of 300; 1.2% at 1000 seeds: spread across tests, within binomial
noise of 1% plus Bonferroni slack used up by MAD-based df); 0.7-1.0% with t(4) noise and 1.3% with
t(3) noise (1000 seeds: t(3) 1.6%, t(4) 0.7%, M=30 t(4) 0.9%). Prewhitening alone took t(3) from
5.3% to 2.1% (1000 seeds; the long-episode share from 3.7% to 0.5%); the fit-noise correction of
the Gumbel bar then halved the spike share (1.0% -> 0.5%). Detection is >= 99% for all three planted
kinds in every scenario (one transient of 300 missed at phi = 0.9, see Excursion tests). Seeded
tests pin the acceptance case (100 members, 3 planted, exactly those named with their kinds), the
homogeneous false-alarm bound (<= 2 of 30 fleets at M = 30 and 100), heavy-tail behaviour, and
no long-episode alarm in 30 t(3) fleets.

## Out of scope (follow-ups)

- Clustering members into k behaviour groups (heterogeneous fleets): lkn.10.
- Series x time heatmap sorted by deviation; small multiples of the top-k outliers: lkn.11.
- Browser / e2e check of the panel: lkn.12.
- Using the `bucket_state` companion (partial buckets, unknown spans, ended vs silent): lkn.13.
- Name-encoded dimensions (2as.16): members whose identity is in the metric name; today members
  are told apart by labels only (a regex over `__name__` works when units agree).
