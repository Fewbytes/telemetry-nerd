# Fleet analysis: `fleet` (bead lkn.3)

"Show me the CPU of all nodes": many series of one metric are analysed as a GROUP (a fleet) instead
of being drawn as 100 lines. Spread per step, outlying members with evidence, churn and missing
data. Builds on robust centre/scale (stability), autocorrelation / n_eff (autocorr, lkn.1), the
series-budget rule (MVP spec §6.3: > 12 series -> group band + outliers) and the missing-data
semantics of the series-bundles spec (§5.2, §7.1: alive / reporting / silent).

## Principles

Principles 4, 8, 9, 10, 11 and 14 apply (`docs/principles.md`). Consequences for this op:

- **The band is an SPC reference** (principle 8): stable, robust, pooled, the centre and sigma the
  tests use; flags come only from the family-wise tests (principle 14), never from the zones. The
  quantile view is descriptive with missing-member bounds (principles 9, 11).
- **No percentile aggregation across members** (principle 10). Percentile datasets (representation `quantile`,
  `histogram_quantile` / `quantile_over_time` expressions, summary series with a `quantile` label)
  are refused with a hint: compare per-member *rates* or *threshold fractions* (fraction_over), or
  the histograms. The median of member p99s is not the fleet p99.
- The fleet at a step is a **population, not a sample**: per-step quantiles across members are
  descriptive (no interval), computed only over members that reported at that step. Missing members
  reduce n; nothing is imputed, interpolated or carried forward (principle 11). n per step is returned and drawn.
- Outliers are judged **relative to the fleet**, with robust statistics that the outliers cannot
  move (median / MAD, leave-one-out), and with the multiple comparisons across members x tests x
  steps controlled (family-wise 1%; principle 14).
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

## Band: the SPC reference (default view; bead nq6)

User decision 2026-10-03: the band must be **stable** to be usable; it need not be "accurate"
(any envelope of a population is somewhat arbitrary). So the drawn band is not the raw per-step
quantiles (they jitter with n and are not what the tests use) but the reference the outlier tests
judge against:

- **Centre** c_t: the per-step median of all members (the tests' centre, full fleet for drawing).
- **Zones** c_t +- 2 sigma_t and +- 3 sigma_t, sigma_t = the tests' robust sigma (1.4826 MAD x the
  small-sample factor, mean-AD fallback) of the members' deviations from c_t **pooled over +-6
  steps**. On the scale the analysis chose: log scale (every value > 0) makes the band
  multiplicative, c_t x exp(+-k sigma_t), linear additive. Stated in the legend:
  "median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative)".
- **`band_window`** (odd steps from 13, the tests' pool and default, to min(steps, 121); else
  refused, saying the limit): larger pools sigma over +-band_window // 2 steps and smooths the
  centre by a centred moving median over band_window steps (a calmer band for long windows).
  Flags are unchanged. The median is centred, so no phase lag mid-window, but a fleet-wide step
  is spread over +-band_window // 2 steps, fleet-wide peaks shorter than that are cut, and in the
  last band_window // 2 steps the median is one-sided and lags (stated as `smoothing`). Pooling
  works in blocks of steps (at most ~4M values at once), so a wide window does not materialise
  members x steps x window.
- **Flags are not the zones.** A member is flagged only by the family-wise tests (below). The
  dashed flag line is the single-step (spike) bar, the larger of the t bar and the Gumbel tail bar,
  x sigma_t around the tests' own centre and pool (whatever band_window). It is approximate: the
  tests' per-member bar varies with pooling, and they use leave-one-out sigma up to 64 members
  (the key says so on hover); level, change and episode tests flag members whose single steps
  stay inside it.
- **Beyond 3 sigma, unflagged.** Counted, never marked per point: in a normal fleet 0.27% of
  member-steps lie there by chance, so with 100 members about a quarter of the steps would carry
  one (more with heavy tails, and a MAD-based sigma under t(4) puts ~1.4% there), mostly on
  members that are not drawn. The summary and legend give it as a rate: "66 member-steps beyond
  3σ unflagged (0.23%; 0.27% if normal)", with a heavy-tail note when `heavy_tailed_noise`
  holds; a drawn line's point
  there says "outside 3σ, not significant at fleet-wide 1% (k members tested)" on hover. No
  per-member run rules.
- **Widening** (a p-chart). A step is marked (a small wedge at the top) when the fleet widened
  faster than the +-6-step sigma tracks: its count c_t of unflagged members beyond 3 sigma, of n_t
  usable, against p_hat = max(0.27%, the window's own share) (heavy tails are the fleet's shape,
  not widening). Overdispersion (autocorrelated, heavy-tailed members): Pearson phi = mean over
  steps of (c - n p)^2 / (n p (1 - p)), floored at 1, and the test is quasi-binomial, c_t / phi
  against Bin(n_t / phi, p_hat). Chosen over Laney's p' chart, whose normal approximation is far
  off at p ~ 0.3% and counts of 0-5; the scaled binomial keeps the exact discrete tail. Each step
  is judged against p_hat and phi of the other steps (leave-one-out: one widened step would
  inflate its own phi and hide itself). Family-wise over the steps: each at 1% / steps with
  n_t > 0, so the chance of any mark in the window is <= 1%. Null (100 members x 1440 steps,
  150 seeds): AR(0.6) normal 0 marked windows, t(4) 0 (an earlier per-step 1% rule against a
  fixed 0.27% marked ~58% of the steps under t(4)). `widening` {steps, at, note, rule}; per group
  when grouped, and the top-level band lists each group's.
- **Behaviour groups**: each group has its own band, flag bar, counts and widening (its own
  centre and sigma: what its members were judged against), in `clusters.groups[].band` and per
  group in the panel; the whole-fleet band is not drawn (it would sit between the groups) and the
  top-level `band` sums the counts.
- **Measurement error** of each member's value is not propagated into sigma (principle 4): the
  band and the tests treat reported values as exact; stated in both views and the summary.

Summary `band`: basis (the legend text, plus the smoothing when band_window > 13), window_steps,
sigma_median (ratio or difference), flag_threshold_z, outside_3sigma_unflagged {member_steps,
members, note}, caveat, source `common_cause`.

## Spread (per step, raw units; the panel's quantile view)

Over the members reporting at the step (n_t): median, quartiles (n_t >= 5), 10/90% (n_t >= 10),
min/max envelope. Quantiles: linear interpolation between order statistics (Hyndman-Fan 7).
Quantiles commute with monotone transforms, so the band is the same on a log or linear axis.
`alive_t` = members seen at or before t (a member that stops reporting stays alive: silent);
coverage `n_t / alive_t`, and `missing_share` = 1 - sum n_t / sum alive_t.

Descriptive: the fleet is the population, so no sampling interval (principle 9: the claims are
about these members only). **Missing-member bounds** at steps with n_t < alive_t: each quantile of
all alive members is recomputed with the m = alive_t - n_t missing values at -inf (lower bound)
and at +inf (upper bound), Hyndman-Fan 7 over n_t + m values. A bound whose order statistics
reach into the missing ranks is unbounded (m >= the quantile's rank: e.g. 2 of 7 missing leave
q25 unbounded below and q75 unbounded above). The min / max envelope is unbounded on the missing
side ("unknown beyond", never a fake limit). Payload `band_bounds.{q}_lo/_hi` (None = unbounded
where the quantile is drawn), sparse: only the `steps` with members missing, with `missing` per
step; absent when nobody is. Members the source marked stale (churn `marked_stale`) are gone, not
missing, from their stale point on: a positive observation (principle 9) that they have no value
to bound. Trailing silence without a marker still counts as missing (alive, silent).

Summary: median relative spread (IQR / median on log scale as q75/q25), spread in the last vs the
first third, time of the widest spread, min/median n per step, missing_member_bounds (how many
steps carry them), caveat (member error not propagated).

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
- **Both modes**: a level or change outlier can also have excursions beyond its own level. Its z
  minus its own baseline (persistent: 20% trimmed mean; shifted: trimmed means before / after
  `at`; drifting: its least-squares line) is scanned with the same excursion scans and bars;
  runs beyond them are its `episodes` (`beyond_own_level`, peak z relative to that level). The
  plain excursion tests fire on such members trivially (an offset member is beyond every bar), so
  without this a persistent member would carry one window-long "episode". These extra scans run
  only on already-flagged members and are **not calibrated** (bead db0): summary episodes carry
  `calibrated: false`, the panel draws them as muted unlabelled brackets. Null check (clean
  persistent and drifting members, M = 60): normal AR(0.6) noise 0 of 800; t(4) noise 21 of 800
  (2.6%).
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
members differ systematically (sizes, roles, zones). The fleet is then split into behaviour
groups (lkn.10, below); when it cannot be split, the caveat stays with the hint:
`normalise="member"` to compare shapes, or query per sub-group (a label) and analyse each.

## Behaviour groups (lkn.10)

Code: `analysis/fleet_clusters.py`. Runs only when the fleet has `many_outliers` (so a homogeneous
fleet, which trips that < 0.3% of the time, is never split), and must then still pass a test.

- **Features.** Each member's deviation from the fleet d_it (log ratio or difference to the per-step
  median of the others), as 20% trimmed means over 8 time blocks: the level and the shape of the
  deviation, in the scale's own units (no standardisation needed). Members missing half of a block
  are clustered without, then join the nearest group centre on their observed blocks.
- **k-selection rule.** Recursive 2-means (k-means++, 4 restarts, seeded). A split is accepted when
  its cluster index CI = within-cluster SS / total SS is smaller than a single Gaussian's with the
  group's own sample covariance would give (SigClust: Liu, Hayes, Nobel & Marron 2008), and both
  parts keep >= 5 members (a lone deviant member is an outlier, not a group). Each part is tested
  again; at most 6 groups, so at most 11 tests: each runs at 1% / 11 (Bonferroni: inventing a group
  anywhere stays under 1%) by sequential Monte Carlo (Besag & Clifford 1991): up to 1100 seeded
  null simulations, stopped at the first one as tight as the data (then p >= 1/2, not significant);
  accepted only when none is. The covariance includes any between-group spread, so the null is
  wide and the test conservative. With a plain 1% per test (first version) one 30/70 fleet in 300
  had its large group split again at p = 2/201. Ungated on homogeneous fleets the test splits
  0% (M = 30/100, normal, t(3), AR(0.9), and a wide unimodal level spread, sd 30%; 300 each).
- **Explanation.** Labels whose values reproduce the groups, by adjusted Rand index (>= 0.5 to be
  listed; ~0 for labels that name every member, like pod). The best label's values per group are
  reported. When the best label explains the groups at ARI >= 0.8, a member whose label value maps
  (>= 80% of that value's members) to another group is moved there (`assigned_by_label`): behaviour
  cannot place a member that drifts from one group's level to the other's, its label can, and a
  member that behaves like the other group is then named in its own (the interesting case).
  With no explaining label, the note says the difference is in behaviour only.
- **Per-group analysis.** Each group is analysed as its own fleet (same scale and normalisation) at
  family-wise alpha / k, so the error over all groups stays 1%. Outliers are judged against their
  own group and carry `cluster`; caveat `clustered` replaces `many_outliers`. The fleet-level band
  is still the whole fleet; the panel payload adds per-group bands.

## Churn and missing data (series-bundles semantics)

The dataset's `bucket_state` companion (series-bundles spec §5; carried through filters, coarsened
with the values for long ranges) is laid on the fleet grid (lkn.13). Without one (a derived
dataset whose op drops it) the fleet falls back to presence from values and says so
(`companion_dropped`, info).

- **Unknown** (fetch failed, source cannot tell): the member-step's value is dropped and it counts
  neither as reporting nor as missing: it leaves n AND alive, so a failed chunk does not inflate
  `missing_share`, does not trip `members_missing`, and is never a churn edge. Caveat
  `untrusted_data` (same code and meaning as on time panels) located with `where = {spans,
  series}` (series null = every member) and the failure reasons. The tested-member rule (half of
  the alive steps) counts known steps only.
- **Partial** buckets (fewer samples than the member's typical count): values kept, but located as
  `members_partial` (members and spans; `warn` and in `caveats` above 5% of alive member-steps,
  else `info`). Outliers carry `partial_buckets` and, for transients, `episode_partial_buckets`
  per episode: an excursion that rests on half-empty buckets may be a collection artefact.
- `appeared`: first seen after the window start (+ tolerance max(3 steps, 5%)), counting known steps
  only.
- `stopped_reporting`: last seen before the end (same tolerance, known steps), with `last_seen` and
  `state`: `marked_stale` when a staleness marker (bucket_state flag `stale_marker`) is on its buckets
  from the last sample on (the source marked the target or series stale; principle 9: report it
  as "marked stale since T", not as "left": a wider window may show it return), else `silent`
  (alive, no samples, spec §5.2: listed next to the outliers, never silently dropped). Today no
  adapter sets `stale_marker`: Prometheus-family range queries never carry staleness markers
  (`stale_marker_visible` is false for Prometheus, Thanos, Mimir; VictoriaMetrics shows them only
  in raw range vectors), so every stopped member is `silent` and the note says the data cannot tell
  a replaced member from a sick one. Appeared ~ stopped suggests replacement (stated).
- `missing_share` = 1 - sum n_t / sum alive_t and min n per step; caveat `members_missing` when
  > 5% (not `missing_data`: that code is the per-series bucket_state caveat on time panels),
  located: the members with silent steps and the spans where n < alive. `members_skipped` when
  some members have too little data to be tested (listed by count).
- `located` (summary and panel payload): the structured caveats above, plus the bundle's own
  (`companion_dropped`, `member_coverage_unknown`); codes of non-info ones join `caveats`.
- Source-aggregated expressions (`sum(...)`) are one series: refused (< 5 members).

## API

- MCP `fleet(dataset, by=None, scale="auto", normalise="none", band_window=None)` ->
  `{dataset, effective_step, members: {count, tested, untested, named_by, never_reported},
  scale, normalise, band: {...}, spread: {...}, coverage: {n_per_step, alive_max, missing_share},
  churn: {appeared, stopped_reporting: [{member, last_seen, state}], note?}, outliers: [{member,
  labels, kind, direction, score, tests, z, since, since_window_start, offset, change?, at?,
  episodes?, evidence, partial_buckets?, episode_partial_buckets?, cluster?}], located, tests: {
  family_wise_alpha, leave_one_out, member_threshold_z, excursion_thresholds_z, ...}, caveats,
  draw, clusters?}`. At most 10 outliers listed (by score; `more_outliers` counts the rest).
  `clusters` (only when split): `{k, rule, split_tests: [{sizes, cluster_index,
  null_cluster_index_median, p, accepted}], groups: [{id, size, members, level_vs_fleet, tested,
  outliers, caveats, label_values}], fleet_level_outliers, explained_by: [{label, adjusted_rand}],
  note?, assigned_by_label?, unassigned?}`; each outlier then has `cluster`.
- `show(dataset, question, mark="fleet")` (uses the latest `fleet` options incl. band_window,
  else defaults). Payload: `spc` {centre, lo2, hi2, lo3, hi3, threshold_z, threshold_lo/hi,
  window, pool_half, legend, outside3, outside3_note, tested, note}, `band` (quantiles),
  `band_bounds`, per drawn outlier `episodes` [{start_ms, end_ms, peak_z, sustained,
  beyond_own_level}] and `effect` {as: ratio | difference, offset, change?, at_ms?,
  change_per_hour?}; per group `spc` zones.
  - **SPC band + outliers** (default view): +-3 sigma and +-2 sigma as nested fills of one hue
    (outer lighter), the median line, the flag bar thin dashed on both sides, widening wedges
    along the top (hover: the note). The x axis is UTC with 24 h ticks ("09:30 UTC" first, the
    date where the day changes), matching the HH:MMZ times in labels and summaries.
  - **Marks per mode** (<= 6 drawn, Okabe-Ito): persistent / shifted / drifting: the whole member
    line coloured, end label kind + effect ("+38% since 09:10", "shifted +41% at 10:07",
    "drifting +2%/h"; "since window start" when the offset covers the window, the offset over
    the since stretch, or "(window mean)" when it is not). Transient: the line grey (3:1 muted) where inside, only its episode
    segments coloured, each episode bracketed on the time axis, peak z in the label ("spike 6.1σ
    10:22–10:25 (momentary)"; a sustained run is an "episode"). Both: the coloured line plus
    muted, unlabelled episode brackets (uncalibrated, db0).
  - **Spread (quantiles)** (toggle): min-max, 10-90, 25-75 as nested fills, median line,
    missing-member bounds as lighter cells at steps with members missing (unbounded to the plot
    edge; hover lists each quantile's bounds and "min / max: unknown beyond").
  - Both views: a coverage strip at steps where alive members did not report (darker = more
    missing). The key carries the encoding (explanations on hover); the legend only counts and
    caveats ("100 members · 3 outliers · 92-100 reporting per step · 66 member-steps beyond 3σ
    unflagged (≈79 expected if normal) · member measurement error not propagated"); one line per
    drawn outlier (kind, direction, effect or episode).
- The chart validator's `series_budget` refusal for line charts now suggests `fleet`.

## Calibration (seeded simulation)

`uv run python scripts/calibrate_fleet.py --seeds 300` (fleets from `tests/unit/fleet_sim.py`):
288 steps, shared daily load x exp(member level (sd 5%) + AR(1) noise (sd 10%)); planted members:
persistent x1.8, transient x2.7 for 12 steps mid-window, drifting 0 -> x2.2 over the window.
False alarm = share of homogeneous fleets in which ANY member is named (design: 1%). Detection =
the planted member named with the right kind (drifting may be `shifted`).

| scenario | false alarm (any member) | by test level/change/spike/episode/long | detect persistent/transient/drifting | split into groups |
|---|---|---|---|---|
| M=10 AR(0.6) normal | 0.3% | 0.3% / 0.0% / 0.0% / 0.0% / 0.0% | - | 0.0% |
| M=30 AR(0.6) normal | 0.3% | 0.0% / 0.0% / 0.0% / 0.3% / 0.0% | 99.7% / 100.0% / 100.0% | 0.0% |
| M=100 AR(0.6) normal | 0.3% | 0.0% / 0.3% / 0.0% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% | 0.0% |
| M=300 AR(0.6) normal | 0.0% | 0.0% / 0.0% / 0.0% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% | 0.0% |
| M=100 white normal | 2.0% | 0.0% / 0.7% / 0.3% / 0.7% / 0.3% | 100.0% / 100.0% / 100.0% | 0.0% |
| M=100 AR(0.9) normal | 0.7% | 0.3% / 0.3% / 0.0% / 0.0% / 0.0% | 100.0% / 99.7% / 100.0% | 0.0% |
| M=100 AR(0.6) t(4) | 1.0% | 0.0% / 0.3% / 0.0% / 0.0% / 0.7% | 100.0% / 100.0% / 100.0% | 0.0% |
| M=30 AR(0.6) t(4) | 0.7% | 0.0% / 0.7% / 0.0% / 0.0% / 0.0% | 99.3% / 100.0% / 100.0% | 0.0% |
| M=100 AR(0.6) 10% missing | 0.3% | 0.0% / 0.0% / 0.0% / 0.3% / 0.0% | 100.0% / 100.0% / 100.0% | 0.0% |
| M=100 no heterogeneity | 1.3% | 1.0% / 0.3% / 0.0% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% | 0.0% |
| M=100 AR(0.6) t(3) | 1.3% | 0.0% / 0.7% / 0.7% / 0.0% / 0.0% | 100.0% / 100.0% / 100.0% | 0.0% |

Two sizes 30/70 (members 0-29 twice the size, `size` label; planted persistent and transient
in the small group, drifting in the large): split exactly 100.0%, planted
named in their group with their kind 99.7%, nothing else named 99.7%.
The last column: homogeneous fleets split into groups (design: never).

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

- Series x time heatmap sorted by deviation; small multiples of the top-k outliers: lkn.11.
- Browser / e2e check of the panel: lkn.12.
- Name-encoded dimensions (2as.16): members whose identity is in the metric name; today members
  are told apart by labels only (a regex over `__name__` works when units agree).
