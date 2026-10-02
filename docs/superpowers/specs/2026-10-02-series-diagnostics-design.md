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

1. **Frequency.** `spectrum()` (Lomb-Scargle, Baluev FAP) on the same prepared series; only
   `significant` peaks count. Significant periods (≤ 3, each ≥ 2 cycles in range) are removed by
   least-squares harmonic regression before the stability tests, so a daily cycle is not read as
   a level shift or drift.
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
7. **Shape** of per-step values: skewness and excess kurtosis with SEs √(6/n_eff), √(24/n_eff);
   zero share (exact count + Wilson interval on n_eff); bimodality coefficient (> 5/9 flags; a
   level shift also produces it, which the report says).

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
- **In control** iff no detector exceeds its expectation at p < 0.01.
- Minimum baseline: 30 points and n_eff ≥ 10, else the SPC section is `insufficient_data`.

## Verdict (per series)

Primary label, in priority order, with every other label that applies in `also`:

| label | rule |
|---|---|
| `insufficient_data` | < 32 points, > 50% gaps, constant, or n_eff < 10 |
| `level_shifted` | a significant changepoint (p < 0.01), |δ| ≥ 1 σ_within, step model SSE < trend SSE |
| `drifting` | 99% slope interval excludes 0, change over range ≥ 1 σ_resid, trend SSE ≤ step SSE |
| `periodic` | ≥ 1 significant spectrum peak |
| `noisy` | none of the above, but heteroscedastic, heavy-tailed (kurtosis interval > 1) or SPC out of control |
| `stable` | none of the above |

Materiality (1 σ) is a stated parameter: smaller significant shifts and trends are reported as
`minor` findings, not verdicts. Reasons are short strings with the numbers.

## API

- MCP `analyze(dataset, baseline_start?, baseline_end?)` → `{dataset, effective_step, baseline,
  series: [{labels, verdict, also, reasons, n, n_eff, frequency, stability, spc, shape}], skipped,
  caveats, draw}`. Key numbers carry `evidence` statistics (dominant_period, trend_slope,
  level_shift, spc_centre_line, spc_sigma, beyond_3sigma_rate); counts are exact.
- `show(dataset, question, mark="spc", windows=[{start, end, label: "baseline"}])`: the window is
  the baseline (default first half, same function as analyze).
- Panel payload `kind: "spc"`: per series ts, value, centre, lcl/ucl (+ 2σ), violations
  `{ts, value, rules}`, EWMA/CUSUM signal times, baseline window, mode, caveats.
- UI `SpcPlot.svelte`: series line, centre line, 3σ band (2σ faint), shaded baseline window,
  violations as red rings, legend stating baseline, mode (individuals | AR(1) residuals), n, n_eff.

## Validation

Seeded simulations in tests: white noise and AR(1) φ = 0.7 — rule-1 rate within the binomial
interval of 0.0027; run-rule rates within the theoretical per-window rates; EWMA/CUSUM episode
counts consistent with Markov-chain ARL₀; and the MR-based σ baseline (conventional) shown to
over-alarm under AR(1). Synthetic period / step / drift / white / AR(1) / short series get the
right verdict.
