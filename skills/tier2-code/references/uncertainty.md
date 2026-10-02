# Uncertainty recipes

All blocks run in a kernel (a test executes them). `d1` = errors per 5 minutes, `d2` = requests per
5 minutes.

## Moving-block bootstrap of any statistic

Resample contiguous blocks of buckets, recompute the statistic, take quantiles.

Block length: the series' autocorrelation length (first lag where the autocorrelation is within
+-2/sqrt(n) of zero), never below n^(1/3) (the usual default order), never above n/4. Estimate it
from the series the statistic is built on; for a ratio of sums, the per-step contribution
`k - ratio * n`. With n/4 < 2 (fewer than 8 buckets) refuse: no honest interval exists. A
percentile bootstrap with blocks under-covers on short series (under about 30 buckets): say so in
`caveats`, or do not publish. A series with a trend or a daily cycle has long autocorrelation by
construction: remove them first, or the block (and the interval) swallows the whole window.

<!-- run: inputs=d1,d2 show=out1 -->
```python
# overall error ratio (ratio of sums) with a block-bootstrap interval, as one point
import numpy as np
import polars as pl
import telemetry_nerd.tn as tn


def acf_block(x):
    """Block length from the autocorrelation of x (see the text above)."""
    n = x.size
    if n < 8:
        raise ValueError(f"{n} buckets: too few for a block bootstrap")
    xc = x - x.mean()
    acf = np.correlate(xc, xc, "full")[n - 1 :] / (xc @ xc)
    quiet = np.flatnonzero(np.abs(acf[1:]) < 2 / np.sqrt(n))
    length = quiet[0] + 1 if quiet.size else n // 4
    return int(min(max(length, round(n ** (1 / 3)), 2), n // 4))


def block_bootstrap(arrays, stat, block, draws=2000, level=0.95, seed=0):
    """arrays: equal-length 1-d arrays resampled together; stat(*resampled) -> float."""
    n = len(arrays[0])
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(draws, -(-n // block)))
    idx = (starts[:, :, None] + np.arange(block)).reshape(draws, -1)[:, :n]
    boot = np.array([stat(*(a[i] for a in arrays)) for i in idx])
    a = (1 - level) / 2
    return np.quantile(boot, [a, 1 - a])


def per_step(h):
    return tn.dataset(h).group_by("ts_ms").agg(pl.col("avg").sum()).sort("ts_ms")


j = per_step("d1").join(per_step("d2"), on="ts_ms", suffix="_r").sort("ts_ms")
k, n = j["avg"].to_numpy(), j["avg_r"].to_numpy()


def ratio(k, n):
    return k.sum() / n.sum()


point = ratio(k, n)
block = acf_block(k - point * n)  # the ratio's per-step contribution
lo, hi = block_bootstrap([k, n], ratio, block)
span = tn.meta("d1")["step_ms"] * k.size
out = pl.DataFrame({"ts_ms": [int(j["ts_ms"][0])], "avg": [point], "lo": [lo], "hi": [hi]})
tn.put(
    out,
    like="d1",
    step_ms=span,
    unit="ratio",
    uncertainty={"method": f"moving-block bootstrap, block={block}", "level": 0.95},
    caveats=["the window is treated as one stationary sample"],
)
print(f"ratio {point:.5f} [{lo:.5f}, {hi:.5f}], block {block} of {k.size} buckets")
```

## Effective sample size (a mean only)

For n buckets with lag-1 autocorrelation r, the mean of an AR(1)-like series has about
n(1-r)/(1+r) independent observations: its standard error scales with 1/sqrt(n_eff), so an
interval that uses n is too narrow by sqrt((1+r)/(1-r)). This is the mean of one roughly
stationary series, an AR(1) approximation: for a slope use the correction in the fit example, for
a ratio or other statistic use the block bootstrap. Cap n_eff at n (negative r would exceed it),
use a t quantile (n_eff is small exactly when this matters), and detrend or deseasonalize first:
a trend or cycle inflates r. r from a short series is biased low, so n_eff is an upper bound.
Report n_eff in `caveats`.

<!-- run: inputs=d1 show=out1 -->
```python
import numpy as np
import polars as pl
import telemetry_nerd.tn as tn

try:
    from scipy import stats  # in the [analysis] extra

    def tq(df):
        return float(stats.t.ppf(0.975, df))

    method = "t interval on effective n from lag-1 autocorrelation"
except ImportError:

    def tq(df):
        return 1.96

    method = "normal interval on effective n from lag-1 autocorrelation (no scipy)"

d = tn.dataset("d1").group_by("ts_ms").agg(pl.col("avg").sum()).sort("ts_ms")
x = d["avg"].to_numpy()
xc = x - x.mean()
r = float(xc[1:] @ xc[:-1] / (xc @ xc))
n_eff = min(float(x.size), x.size * (1 - r) / (1 + r))
se = x.std(ddof=1) / np.sqrt(max(n_eff, 2.0))
mean, half = float(x.mean()), tq(max(n_eff - 1, 1.0)) * se
out = pl.DataFrame(
    {"ts_ms": [int(d["ts_ms"][0])], "avg": [mean], "lo": [mean - half], "hi": [mean + half]}
)
tn.put(
    out,
    like="d1",
    step_ms=tn.meta("d1")["step_ms"] * x.size,
    uncertainty={"method": method, "level": 0.95},
    caveats=[f"effective n {n_eff:.0f} of {x.size} (lag-1 r={r:.2f}); assumes no trend or cycle"],
)
print(f"mean {mean:.3f} +-{half:.3f}, r={r:.2f}, n_eff={n_eff:.0f}")
```

## Which interval is wrong when

| Situation | Wrong choice | Right choice |
|---|---|---|
| Error ratio, true counts, independent failures | bootstrap on 12 points | Wilson on the counts |
| Bursty errors (many in one incident) | Wilson alone (overdispersed) | block bootstrap over time buckets |
| Ratio from `increase()` (fractional, extrapolated) | Wilson without a caveat | Wilson with the caveat, or block bootstrap |
| Mean of a slow-moving gauge | i.i.d. bootstrap | block bootstrap or n_eff |
| Slope of a cumulative gauge | plain OLS interval | OLS interval corrected by n_eff, or a residual block bootstrap |
| Count from a sampled source | `exact=True` | estimate with the sampling error in `caveats` |
| Heavy-tailed latency mean (infinite variance) | normal approximation or bootstrap | a distribution statistic (fraction over x, a quantile with its n) |
