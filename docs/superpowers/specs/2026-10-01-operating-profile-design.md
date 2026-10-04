# T1 operating profile (2as.7)

What a signal normally looks like over a long window (spec §4.2 T1, §6.2): the basis for the
reference y-range (2as.10), the seasonal normal band (2as.11) and the metric card (2as.12).

## What is profiled
Key `(source, profiled expression)`. `profile_target(expr)` turns what the user looks at into
what is profiled at the profile step (1h):
- bare counter selector (catalog/rule type `counter`) -> `rate(sel[W])`: never the raw total.
- `rate/irate/deriv(sel[w])` windows are widened to `W = $__rate_interval` at the profile step
  (max(4 x profile-source series interval, step + series interval)), so a 1m-rate panel and a 5m-rate
  panel share one profile. Other windowed functions keep their windows (widening `increase`
  or `max_over_time` would change the statistic).
- `histogram_quantile(...)` is profiled as the per-hour quantile series (`kind=quantile`): a
  quantile of each hour's merged histogram; the profile describes the distribution of hourly
  quantiles across hours. Quantiles are never averaged.
- refused (with a hint): histogram buckets as lines, counters used outside a rate-like call.
`kind` is `level` | `rate` | `quantile`; `rate_window_ms` is recorded.

## Data
1h x 30d (configurable) through the series cache (daily refresh re-fetches only the newest
chunk). Bare selectors use the source's rollup path (`*_over_time` / MetricsQL `rollup`), so
each hour carries the true intra-hour min/max at the series interval; other expressions are
evaluated per step (`fetch_values`), and then min = max = value (`extremes: false`).
Window = whole hours ending at the last complete hour. Empty / NaN / count=0 hours are gaps.

**Profile source pairing:** `SourceSpec.profile_source` names another registered source
holding downsampled data (Wikimedia: `wikimedia-1h` = `thanos-downsample-1h`). Profiles use it
when it is live, else the source itself with caveat `profile_source_unavailable`.
`profiled_from` names the source actually used.

## Statistics (per series, plus `pooled` over all series-hours)
- `n` (hours with data), `expected`, `coverage`, `first_ms`/`last_ms`, `history_ms`.
- Range over hourly means: min, p0.5, p25, p50, p75, p99.5, max, MAD (unscaled). Quantiles
  are numpy type 7 (linear) over the hourly values.
- `envelope` = [p0.5 of hourly minima, p99.5 of hourly maxima]: the robust range a
  series-interval-step panel would show; `min`/`max` are absolute (min of minima, max of maxima).
  Without intra-hour extremes the envelope equals the hourly range (caveat
  `no_intra_hour_extremes`); for rates it is at the query window's width.
- Caveats: `short_history` (data covers < 90% of the time range), `gaps` (coverage within the
  history < 90%), `low_n_tails` (n < 200: p0.5/p99.5 are essentially the extremes),
  `looks_like_counter` (a level that rises in > 95% of hours: probably an unrated counter),
  `non_finite` (hours dropped for NaN/Inf), `quantile_series`.

## Seasonal profile
Hour index in UTC (Monday 00:00 = 0), from each bucket's start (ts - step). Candidate models:
`none` (one global median), `hour_of_day` (24), `hour_of_week` (168). Level = median of the
bucket. A model is eligible when >= 90% of its buckets have >= 3 samples (hour_of_week with
30d has ~4 per bucket: we do not fake it with 1-2). The model is chosen by **leave-one-out
mean absolute error** (each hour predicted by its bucket's median without itself); a richer
model must beat the simpler one by >= 2%. Scores are reported (`scores`).

Band = level + quantiles of the chosen model's LOO residuals: honest prediction errors for a
new week, not in-sample residuals that shrink with few samples per bucket. Residuals are
pooled per hour of day when every such pool has >= 20 residuals (daily heteroscedasticity),
else globally (`residual_pool`). Band quantiles are Weibull (type 6): the order-statistic
interval covers a new draw with exactly the nominal probability, where type 7 under-covers small
pools (28 residuals: ~84% for a nominal 90%). Checked over 60 seeds of 4 weekly cycles + noise:
held-out coverage 93% (slightly conservative: residuals are predicted from n-1 samples while the
level uses n). Per bucket: `n, level, lo, hi` (central 90%) and
`q25, q75`. Buckets with < 3 samples carry `level: null` (`sparse_buckets`). No stddev
anywhere. Bands are not clipped to natural bounds: consumers do that (they know `bounds`).
`band_at(seasonal, ts_ms)` looks up the bucket for any timestamp. Seasonal profiles are
computed for at most 12 series; beyond that only ranges (caveat `seasonal_series_capped`).

## Laziness and refresh (`core/profiles.py: ProfileService`)
- `cached(source, expr)`: sync read, never computes; carries `stale` (age >= 24h).
- `ensure(source, expr, force=False)`: compute when missing/stale/forced; single-flight per
  key; a failure is stored and not retried for 1h (`ProfileUnavailable` with the reason).
- `request(source, expr)`: schedule a background `ensure` if missing/stale; returns cached.
  `TelemetryService.show` calls it for time-series panels when `auto_profile` is on (daemon).
- Store: `operating_profiles` table in the workspace DB, id `op-<hash>`. For a bare metric
  name in the catalog, the catalog gets an `operating_profile_ref` claim (origin `stats`).
  One internal `profile.computed` event per computation.
- MCP: `operating_profile(expr, source)`: compact summary (no bucket arrays).

## Local-time seasonality (2as.24)
`SourceSpec.timezone` (IANA, default UTC; `source_connect(timezone=...)`) sets the clock the seasonal
buckets count in. Hour-of-day and hour-of-week are taken on the source's wall clock (DST-aware, floor
for half-hour zones), so a 09:00 ramp stays in one bucket across a DST change instead of smearing two
UTC buckets. `Seasonal.tz` and `ProfileStats.tz` record it, `band_at`/`seasonal_shape` read it, and a
stored profile whose tz differs from the source's is stale and recomputed on next view. The timezone is
configured, not learned: guessing it from load shape is unreliable for global services.
