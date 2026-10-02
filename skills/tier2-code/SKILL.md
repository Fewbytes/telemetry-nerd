---
name: tier2-code
description: This skill should be used when no tier-1 tool (query, analyze, fleet, query_distribution, fraction_over, compare_seasonal) answers a telemetry question and custom Python is needed, such as a ratio with a confidence interval, a custom statistic with a bootstrap, a regression or capacity projection ("when will it fill"), a join of two datasets, or when asked to "run_code", "rerun a code node", "read why a code node failed", "declare uncertainty for a result", "put_fit", or "promote this snippet to a tool". Covers tier-1-first routing, declaring inputs, uncertainty rules (unknown is citable but flagged, errors propagate maximally), the tn API, fits, and failure handling with code_get.
---

# Tier-2 code: `run_code`

`run_code` runs Python in the workspace's persistent IPython kernel against datasets already in
the workspace. It is the long tail: a statistic or model that no tier-1 tool computes. Every run
is a **code node** (`c1`, `c2`, ...) with lineage: the stored code, its input datasets and its
output datasets, so a result stays inspectable and re-runnable. The kernel is not sandboxed: it
is equivalent to running Python in Bash.

## Tier-1 first

Check this table before writing code. Tier-1 results carry validated uncertainty, caveats and
context for free.

| Question | Tool |
|---|---|
| What does this metric look like / is it normal? | `query`, `show`, `operating_profile` |
| Stable, drifting (trend with an autocorrelation-aware interval), level-shifted, periodic? | `analyze` |
| Unusual for this hour or weekday? | `compare_seasonal` |
| What periods does it contain (cron, GC)? Remove them | `spectrum`, `filter` |
| How do many pods/nodes behave as a group; which are outliers? | `fleet` |
| Latency distribution, fraction of REQUESTS over x, heatmap | `query_distribution`, `fraction_over`, `show` |
| Latency of successes vs failures | `split_outcome` |
| Current window vs previous / last week | `analyze(baseline=...)`, `show_marginal`, `set_overlays` |

A ratio of two metrics is a PromQL expression: `query("sum(errors) / sum(requests)")` returns it
as a dataset. Use `run_code` only for what the query language cannot give: the interval around
the ratio, a statistic of your own, a model.

Use `run_code` when the question needs: an interval for a derived quantity, a custom statistic or
its bootstrap, a regression or capacity projection beyond `analyze`'s trend (projection to a
threshold, a custom model), a join across datasets, a custom transform.

Not for: fetching data (the code cannot reach the sources: `query` first), percentile series as
input (a percentile of percentiles is not a percentile: query the histogram), or re-deriving
what a tier-1 tool returned.

## Procedure

1. **Check the table above**; if a tier-1 tool answers, use it and stop.
2. **Get the data into datasets.** `query` / `query_distribution` / `show` first; note the handles
   (`d3`). Look at a panel before computing: a chart catches wrong joins and gaps.
3. **Declare inputs up front**: `run_code(code, inputs=["d3", "d5"])`. They are exported before
   the run; reading an undeclared handle fails.
4. **Never paste bulk data** into code or the answer, and never print rows. Read with
   `tn.dataset`, print a few aggregate numbers, store results with `tn.put`.
5. **Declare uncertainty or exactness on every output** (next section).
6. **Draw the result** with `show(dataset)` using `outputs[].dataset` from the result. Name the
   code node (`c4`) in the reply.
7. **Cite it** with `finding_create` (below), and report any uncertainty flag it returns.

Defaults: `timeout_s` 120 s (a larger value has a maximum). The result lists `issues` for outputs
that were not ingested (uncommitted, invalid): read them before assuming the output exists.

## Declaring uncertainty

| Output | Declare | How |
|---|---|---|
| Ratio of raw event counts, failures roughly independent | Wilson score interval | `uncertainty={"method": "Wilson score", "level": 0.95}` + `lo`/`hi` |
| Any statistic of a time series, or counts that cluster (incident bursts) | Moving-block bootstrap, block = max(autocorrelation length, n^(1/3)) | same; `method` names the block size; helper in `references/uncertainty.md` |
| Mean of a series with AR(1)-like correlation | Normal/t interval on effective n = n(1-r)/(1+r), capped at n | `references/uncertainty.md`; not for slopes or ratios |
| Slope or trend | Correct by n_eff (as in the fit example) or a residual block bootstrap | `put_fit` |
| Count taken from raw data: number of series, bucket totals, discrete events | `exact=True`, no lo/hi | never for rates, means, ratios, `increase()`/`rate()` output, or sampled counts |
| Future values | Prediction interval, `kind="prediction"` | `put_fit(prediction=..., prediction_meta=...)` |

When both Wilson and the bootstrap apply (an hourly error ratio is both a count ratio and a time
series), use Wilson only if the counts are true event counts and failures are independent;
otherwise bootstrap over time buckets, or compute both and publish the wider.

Rules that keep results honest:

- An i.i.d. bootstrap on a time series is wrong: neighbouring buckets are correlated, so the
  interval is too narrow. Resample blocks, and size them from the data, not from habit.
- `exact=True` claims that nothing is estimated. `increase()` extrapolates and is fractional, so
  it is not exact. Sampled counts carry relative error about sqrt((1-p)/(pN)) (p the sampling
  rate, N the true count: plug in observed count / p).
- An output tagged `no_uncertainty` (`uncertainty_status` in the result) has *unknown*
  uncertainty, not zero: derive an interval first (bootstrap, effective n, bucket bounds,
  Wilson) and re-run. If none can be derived it can still be cited, and the finding is marked
  "uncertainty unknown" (cite the value with `uncertainty_unknown: true`): say so when you
  report it, and never present it as exact.
- Name the method in words (`"moving-block bootstrap, block=12"`): the interval is only as good
  as that sentence. Put assumptions the reader must know in `caveats` (bucket midpoints,
  fractional counts, a stationary window). An interval does not cover bias from a modelling
  shortcut: say so in the caveat.

## Fits

Store models with `tn.put_fit`, never as a bare print. Parameters carry intervals
(`{"value": v, "interval": [lo, hi]}`, or `{"value": n, "exact": True}` for an integral value),
and `diagnostics` are mandatory: the checks that show whether the assumptions hold (Durbin-Watson
and lag-1 autocorrelation of residuals, residual spread, n, skew/kurtosis). Read them before
quoting the fit. If they fail, correct the interval (n_eff, block bootstrap of residuals) or
say the band is optimistic. A prediction is a series with a prediction band, stored with the fit.
A fit has no panel of its own: `show` its `<fit>_prediction` dataset, cite parameters as
statistics. Uncertainty is per parameter: one without an interval is cited as stored with
`uncertainty_unknown: true` and flagged; the others are unaffected.

## Citing results

```python
finding_create(
    claim="Disk grows 0.48 GB/h (95% CI 0.43 to 0.53)",
    scope={
        "source": "default",
        "selector": "disk_used_bytes",
        "start": "now-12h",
        "end": "now",
        "step": "5m",
        "aggregation": "mean",
    },
    evidence=[
        {
            "kind": "statistic",
            "dataset": "d6",
            "name": "slope_per_hour",
            "value": 0.48,
            "interval": [0.43, 0.53],
            "method": "OLS, n_eff-corrected",
        }
    ],
)
```

For a fit parameter, `name` must be the parameter's name and value/interval/exact must match
what was stored. A statistic named like a percentile (p95, median) needs `params={"q": 0.95,
"n": <observations>}`. Only fabrication is rejected (a parameter not cited as stored, a fit cited
as a panel). Uncertainty problems come back as flags in the result
(`uncertainty: [{evidence, flag, message}]`: `uncertainty_unknown`, `input_uncertainty_unknown`,
`uncertainty_not_propagated`), are stored on the finding and shown to the user: quote them.

## When a run fails

`run_code` returns a short stdout and the tail of the traceback. To read more, call
`code_get(code_node, part="traceback")` (or `stdout`, `stderr`, `code`; default `all`), paging
with `offset` while `next_offset` is not null. Fix the code and call `run_code` again (a new
node), or `rerun_code(code_node)` when the code was right and the cause was external (kernel
restart, a corrected input). Finished nodes are never edited; a re-run is a new node with
`rerun_of`.

If the result has `restarted: true`, variables from earlier runs are gone: re-create state from
the inputs and never rely on globals across runs. A timeout fails the node: make the computation
cheaper (fewer bootstrap draws, thinner data) before raising `timeout_s`.

## Promote what recurs

When the same snippet is written a second time, it belongs in the tier-1 toolset. File a bead
(`bd create`; elsewhere an issue, or tell the user) titled "Promote <what it computes> to a tier-1
op" with the code node id, the question it answered and the uncertainty method. Do not
re-implement tier-1 behaviour inside `run_code`.

## `tn` cheat-sheet

```python
import telemetry_nerd.tn as tn

tn.inputs  # handles declared for this run (works inside a run only)
df = tn.dataset("d3")  # polars; tn.dataset("d3", arrow=True) gives pyarrow
tn.meta("d3")  # unit, step_ms, representation, caveats, uncertainty, start_ms, ...
```

- Time series rows: `ts_ms, series_id, avg, min, max, count` (+ `lo, hi` when the input declared
  an interval). Distribution rows: `ts_ms, series_id, bucket_lo, bucket_hi, count` (lowest bucket
  starts at -inf, top one ends at +inf). Other tables: `tn.dataset(h, "series")` (series_id,
  labels JSON), `"columns"` (distribution step totals).
- `tn.put(df, meta=None, *, columns=None, **meta_keys) -> name`: store a series. `df` needs
  `ts_ms` and `avg`, plus `series_id` for several series (or `labels=[cols]`), plus `lo`/`hi`
  when declaring `uncertainty`. Keys: `like="d3"` (inherit step, range, unit; time series only),
  `representation`, `step_ms`, `start_ms`, `end_ms`, `unit`, `description`, `caveats`,
  `parents`, `uncertainty={"method", "level", "kind"}` or `exact=True`, `name` (lowercase;
  default `out1`, `out2`). `kind`: `confidence` (default), `credible`, `prediction`, `tolerance`.
- A one-number summary is a one-row series: `ts_ms` = window start, `step_ms` = the whole span,
  `start_ms`/`end_ms` = the window (see the worked examples).
- `tn.put_fit(model, params, *, method, diagnostics, goodness=None, level=None, prediction=None,
  prediction_meta=None, name=None, parents=None, caveats=None, start_ms=None, end_ms=None)`:
  store a fit (default name `fit1`).
- Errors raise `tn.TnError` saying what to fix (unknown column, undeclared handle, missing
  `lo`/`hi`): read the message, fix, re-run.
- numpy and polars are always available; scipy, statsmodels, scikit-learn, ruptures and pywt only
  with the `[analysis]` extra: import inside the code, handle `ImportError`, and say what is
  missing.

## Additional resources

- **`references/worked-examples.md`**: hourly error ratio with a Wilson band; share of latency
  time above a threshold with a block bootstrap; linear fit with n_eff-corrected intervals,
  prediction band and time-to-full.
- **`references/uncertainty.md`**: the block-length and block-bootstrap helpers, effective sample
  size for a mean, and which interval is wrong when.
