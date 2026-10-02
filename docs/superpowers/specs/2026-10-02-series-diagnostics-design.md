# Series diagnostics: `analyze` (bead lkn.1)

One call that runs the right checks on a time series and returns a compact, evidence-ready
report plus an SPC panel. Downstream: seasonal comparison (lkn.2) reuses the autocorrelation /
ESS / harmonic-fit / control-limit blocks; fleet analysis (lkn.3) reuses robust centre/scale,
run rules and EWMA/CUSUM on per-series deviations.

## Principles (carried over)

- Preconditions as for `spectrum`: percentile series, distributions and raw counters are refused
  with hints (`time_op_problem`); gaps are never interpolated: autocorrelation uses only pairs
  of observed points `k` steps apart, run rules break at gaps.
- Every statistic carries an interval or a stated n (exact counts). Intervals use the
  **effective sample size** (n_eff = n / τ_int), never the raw n.
- SPC limits come from a **stated baseline window**, never from the data being judged. Default
  baseline: the first half of the dataset range; the judged window is everything outside it.
- Verdict says `insufficient_data` instead of guessing.
- No scipy: numpy + `statistics.NormalDist` + small closed forms (documented below).

## Modules (pure, no I/O)

| module | content | reused by |
|---|---|---|
| `analysis/autocorr.py` | gap-aware ACF, τ_int (Geyer initial positive sequence), n_eff, AR(1) fit and residuals | lkn.2, lkn.3 |
| `analysis/stability.py` | harmonic (seasonal) fit, trend with AR-adjusted interval, CUSUM changepoints (binary segmentation), KPSS, variance ratio, distribution shape | lkn.2 |
| `analysis/spc.py` | robust baseline limits with intervals, run rules, EWMA, CUSUM, in-control ARL by Markov chain, expected false alarms | lkn.3 |
| `analysis/diagnostics.py` | composes the above per series; verdict + reasons + evidence | service |
| `core/signal_ops.py` | `analyze_summary` (MCP) and `spc_panel` (UI payload), memoised | |

## Checks

1. **Frequency.** `spectrum()` (Lomb-Scargle, Baluev FAP) gives white-noise-significant
   candidates. The white-noise FAP calls AR(1) wandering and the 1/f² power of a step
   "significant", so candidates are **confirmed against an AR(1) red-noise background**
   (φ from the residual after removing all candidates; z = p N / (2 S_AR(f)) ~ Exp(1);
   FAP = 1 − (1 − e^−z)^M, M = N/2), with prewhitening (stronger confirmed periods removed
   first) so sidelobes of one sinusoid are not extra periods. Order: structure on the raw series
   → periods confirmed on what it leaves → harmonics (≤ 3) removed → structure again.
   The standalone `spectrum` tool uses the same test (lkn.4): every peak gets `fap_red_noise`
   (on what the BIC structure model leaves), `significant` needs it < 1%, and the panel draws
   the per-series red-noise 1% level 2 S_AR(f) z*/N (dotted) beside the white-noise level.
2. **Autocorrelation.** ACF from observed pairs; τ_int = 1 + 2 Σ ρ_k by Geyer's initial positive
   sequence (lags ≤ n/4); n_eff = n / max(τ, 1) (never more than n). Lag-1 ρ reported with ±2/√n.
3. **Trend.** OLS slope on the de-seasonalised series; SE inflated by √τ of the OLS residuals;
   99% interval with Student-t (Cornish-Fisher expansion, df = n_eff − 2). Reported per hour and
   as change over the range.
4. **Level shifts.** CUSUM of deviations from the mean, statistic sup|S_k| / (σ_lr √n) against the
   Kolmogorov distribution (sup of a Brownian bridge). σ_lr = σ_e / (1 − φ) from an AR(1) fit to
   the residuals of the split model (so the shift itself does not inflate it). Binary
   segmentation, ≤ 3 changepoints, segments ≥ 8 points, α = 0.01 per test. Each shift: size with
   interval (σ_lr √(1/n₁ + 1/n₂)), time, p.
5. **Stationarity.** KPSS (level) on the de-seasonalised series, Newey-West long-run variance with
   l = ⌊12 (n/100)^¼⌋; critical values 0.347 / 0.463 / 0.574 / 0.739 (10/5/2.5/1%). Reported;
   used as supporting evidence only (KPSS over-rejects under strong AR(1)).
6. **Heteroscedasticity.** Robust scale (MAD) of model residuals, last third / first third, with
   a log-normal interval from var(log σ̂_MAD) ≈ 1.36 / n_eff per third.
7. **Shape** of per-step values: skewness and excess kurtosis with moving-block bootstrap
   intervals (blocks ≈ 2τ; the normal-theory √(6/n) is far too narrow for skewed data); zero
   share (exact count + Wilson on n_eff); bimodality coefficient > 5/9 **and** negative excess
   kurtosis (BC alone fires on any strongly skewed law); `bimodal_from_shift` caveat.

## SPC

- **Baseline model.** Optional seasonal component (harmonics of significant periods, fitted on the
  baseline only, only if the baseline spans ≥ 2 cycles). Centre = baseline median (+ seasonal
  curve); σ = 1.4826 · MAD of baseline deviations: the **marginal** σ, robust to baseline outliers.
  Intervals: centre ± z · 1.2533 σ / √n_eff; σ · exp(± z √(1.36 / n_eff)); limits inherit both.
- **Autocorrelation.** Conventional individuals charts estimate σ from the moving range, which
  under positive autocorrelation underestimates the marginal σ and floods the chart with false
  alarms. Here (a) the drawn limits are centre ± 3 marginal σ ("widened" relative to MR), so the
  per-point false-alarm rate stays 0.27% under any stationary dependence; (b) when the baseline
  lag-1 φ is significant (|φ| > 2/√n), the sequence detectors (run rules, EWMA, CUSUM) run on
  AR(1) residuals e_t = d_t − φ d_{t−1} (only where t−1 was observed), standardised by their own
  baseline MAD. Near φ ≈ 1 the residual chart has low power for sustained shifts (≈ (1−φ)δ):
  caveat `near_random_walk`.
- **Rules** (counted over judged points, windows ending at the point, broken by gaps):
  1. beyond 3σ (raw series, marginal σ): rate 0.0027;
  2. 2 of 3 beyond 2σ, same side: 2(3p²(1−p) + p³), p = P(Z > 2);
  3. 4 of 5 beyond 1σ, same side: 2(5q⁴(1−q) + q⁵), q = P(Z > 1);
  4. 8 in a row on one side: 2 · 2⁻⁸.
  Each: count, expected count under control, Wilson interval on the rate.
- **EWMA** λ = 0.2, L = 3 (asymptotic limits) and **two-sided CUSUM** k = 0.5, h = 5 on the
  standardised sequence; statistics restart after each signal, so signals are episodes. In-control
  ARL₀ by Brook-Evans Markov chains (EWMA: Lucas-Saccucci, 2·100+1 states; CUSUM: 200 states,
  two-sided 1/ARL = 1/ARL⁺ + 1/ARL⁻). Expected episodes = n_judged / ARL₀; p = Poisson tail.
- **In control** iff none of the deciding detectors (rule 1 on the standardised sequence, EWMA,
  CUSUM) exceeds its expectation at p < 0.01/3. p-values use a conservative scenario for the
  baseline's estimation error (σ at its one-sided 95% upper bound, centre off by its 95% bound)
  so a short baseline does not manufacture alarms. Rules 2–4 count overlapping windows: reported
  with expected counts, no p-value. The drawn-band exceedances (raw, marginal σ) are reported
  with a Wilson interval on n_eff (they cluster under autocorrelation).
- Minimum baseline: 30 points and n_eff ≥ 10, else the SPC section is `insufficient_data`.

## Reference baselines, seasonal residual charts, gaps (lkn.5)

- **`baseline="previous" | "day" | "week"`** (`baseline_cycles` 1..4 / 1..7 / 1..4): the limits
  come from a **separate fetch** of earlier windows (same expr, step, source, through the cache),
  aligned exactly as `compare_seasonal` does (`analysis/seasonal.cycle_shifts`: `previous` = the
  preceding windows of the same length, `day` / `week` = the same window on previous local
  calendar days / weeks in `tz`, 23 h / 25 h across DST). Every point of the dataset is judged.
  The reference is prepended to the series for the chart (positions on the common step grid, so
  pairs, AR(1) residuals and run windows never span the gap between them) and the chart is then
  cut back to the dataset (`ControlChart.tail`). Output states the windows and their datasets;
  `show(mark="spc")` after such an `analyze` uses that reference (stored on the layer as `spc`).
- **Seasonal centre from the operating profile** (2as.7). A baseline that holds fewer than 2
  cycles cannot fit a daily / weekly cycle; then, if the cached operating profile of the
  expression is seasonal (`hour_of_day` / `hour_of_week`), the centre is
  `level + shape(t)`: the profile's per-bucket medians at their hour midpoints, joined linearly
  around the cycle (sparse buckets bridged), minus their median. Level and σ still come from
  the baseline only (median / MAD of y − shape), so the chart is a seasonal residual chart.
  "Holds 2 cycles" uses the time the baseline actually observed (several 6 h same-phase windows
  span days but observe hours). **The shape never sees the judged data**: the profile's seasonal
  model is re-fitted (`seasonal_profile`, model re-chosen) from its hourly history in the series
  cache with every hour that overlaps the dataset removed (`ProfileService.seasonal_excluding`,
  sync, never fetches; reported as `judged_hours_excluded`). Without a cached profile nothing
  changes (`seasonal_not_in_baseline` as before). Dataset harmonics (short periods the baseline
  holds twice) are fitted on y − shape on top; dropped periods the profile's cycle or one of its
  harmonics (≥ 2 h) explains no longer raise `seasonal_not_in_baseline`. The shape's own error
  (bucket medians of ~30 hourly values) is not in the centre interval; it is small next to σ
  for hourly-or-finer data (≈ 1.25 σ_hour / √30).
- **Gaps in the sequence detectors.** Across g missing steps (a gap, or a baseline stretch
  between judged parts) the EWMA decays by (1 − λ)^g, the time-aware EWMA with missing points
  given zero weight, and each CUSUM side drains by g·k, its in-control drift per step; nothing is
  imputed. No gap = the classic recursion, a long gap (≈ 20 steps for the EWMA, h/k = 10 for the
  CUSUM) = a restart. Both only shrink the statistic, so the in-control ARL can only grow
  (conservative); expected episodes stay n_judged / ARL₀.
- **Calibration** (seeded, in `tests/unit/test_spc.py`): 10% of judged points missing in runs of
  1–30: EWMA ARL ≈ 537 / 490 (white / AR(1) 0.7; theory 558), CUSUM ≈ 469 / 423 (465), run-rule
  rates 0.96–1.07× theory, 0/150 decided out of control. 6 h of 1-min data on a daily cycle
  (amplitude 3σ) against the preceding 6 h or the same 6 h last week, shape from 30 noisy days:
  rule-1 rate 1.25–1.3× nominal (limits estimated from 360 points), run rules 1.05–1.2×, EWMA /
  CUSUM ARL ≈ 450 / 350, 0% decided out of control; the flat centre is unusable there (baseline
  n_eff < 10 in most seeds, ~40% of points outside the band in the rest).

## Verdict (per series)

Primary label, in priority order, with every other label that applies in `also`:

| label | rule |
|---|---|
| `insufficient_data` | < 32 points, > 50% gaps, constant, or n_eff < 10 |
| `insufficient_data` (also) | n_eff < 10 after removing structure (near-unit-root series) |
| `level_shifted` | significant changepoint(s) (p < 0.01), \|δ\| ≥ 0.25 σ_within, step model has the lowest BIC |
| `drifting` | 99% slope interval excludes 0, change ≥ 0.25 σ_resid, trend model has the lowest BIC |
| `periodic` | ≥ 1 period confirmed against red noise |
| `noisy` | none of the above, but heteroscedastic, heavy-tailed (kurtosis interval > 1) or SPC out of control |
| `stable` | none of the above |

Materiality (0.25 σ; "small" below 1 σ) is a stated parameter: smaller significant shifts and
trends are reported as `minor`; an out-of-control chart explained by a (minor) shift or trend is
not "noisy". Reasons are short strings with the numbers.

## API

- MCP `analyze(dataset, baseline_start?, baseline_end?)` → `{dataset, effective_step, baseline,
  series: [{labels, verdict, also, reasons, n, n_eff, frequency, stability, spc, shape}], skipped,
  caveats, draw}`. Key numbers carry `evidence` statistics (dominant_period, trend_slope,
  level_shift, spc_centre_line, spc_sigma, beyond_3sigma_rate); counts are exact.
- `show(dataset, question, mark="spc", windows=[{start, end, label: "baseline"}])`: the window is
  the baseline (default first half, same function as analyze).
- Panel payload `kind: "spc"`: per series ts, value, centre, lcl/ucl (+ 2σ), violations
  `{ts, value, rules}`, EWMA/CUSUM signal times, baseline window, mode, caveats.
- UI `SpcPlot.svelte`: series line (null rows at gaps), centre line, 3σ band, shaded baseline
  window, violations (filled: deciding rules, hollow: supplementary run rules), legend stating
  baseline, mode (individuals | AR(1) residuals), n, n_eff.

## Validation

Seeded simulations in tests: white noise and AR(1) φ = 0.7 — rule-1 rate within the binomial
interval of 0.0027; run-rule rates within the theoretical per-window rates; EWMA/CUSUM episode
counts consistent with Markov-chain ARL₀; and the MR-based σ baseline (conventional) shown to
over-alarm under AR(1). Synthetic period / step / drift / white / AR(1) / short series get the
right verdict.
