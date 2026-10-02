# Worked examples

Each block runs as-is in the kernel (a test executes them). The first comment says which handles
it expects: substitute the handles `query` / `query_distribution` returned in the current
workspace. Each ends by storing its result with a declared uncertainty or fit; then `show` the
stored dataset. The block-bootstrap helpers are in `uncertainty.md`.

## A. Error ratio per hour, with a Wilson band

Question: "what fraction of requests failed each hour, and how sure are we?" The ratio itself is
a PromQL expression (`query("sum(increase(errors_total[5m])) / sum(increase(requests_total[5m]))")`);
what `query` cannot give is an interval. Wilson applies when k and n are true event counts and
failures are roughly independent; if errors arrive in incident bursts, use the block bootstrap
instead (`uncertainty.md`).

Setup: `query("sum(increase(errors_total[5m]))", step="5m")` -> d1 and the same for requests
-> d2. Each 5-minute bucket holds one sample, so `avg` is that bucket's increase.

<!-- run: inputs=d1,d2 show=out1 -->
```python
# expects d1 = errors per 5 min, d2 = requests per 5 min (same step)
import numpy as np
import polars as pl
import telemetry_nerd.tn as tn

HOUR = 3_600_000
step = tn.meta("d1")["step_ms"]
assert step == tn.meta("d2")["step_ms"], "align the two queries first"
per_hour = HOUR // step


def hourly(df, name):
    steps = df.group_by("ts_ms").agg(pl.col("avg").sum().alias(name))  # sum over the series
    return (
        steps.with_columns(ts_ms=pl.col("ts_ms") // HOUR * HOUR)
        .group_by("ts_ms")
        .agg(pl.col(name).sum(), pl.len().alias(f"{name}_steps"))
    )


def wilson(k, n, z=1.96):  # 95% score interval for k successes out of n trials
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return centre - half, centre + half


j = hourly(tn.dataset("d1"), "k").join(hourly(tn.dataset("d2"), "n"), on="ts_ms")
# whole hours only: the first and last are usually partial (fewer steps)
j = j.filter((pl.col("n") > 0) & (pl.col("k_steps") == per_hour)).sort("ts_ms")
n = j["n"].to_numpy()
k = np.clip(j["k"].to_numpy(), 0, n)  # increase() extrapolation can overshoot
lo, hi = wilson(k, n)
out = j.select("ts_ms").with_columns(avg=pl.Series(k / n), lo=pl.Series(lo), hi=pl.Series(hi))
tn.put(
    out,
    like="d1",
    step_ms=HOUR,
    unit="ratio",
    description="hourly error ratio, 95% Wilson band",
    uncertainty={"method": "Wilson score", "level": 0.95, "kind": "confidence"},
    caveats=["increase() extrapolates, so counts are fractional: Wilson is approximate"],
)
print(out.height, "whole hours; worst", float((k / n).max()))
```

## B. Share of latency time spent above a threshold (no percentile)

Question: "how much of the total time users spend waiting is waiting on requests slower than
1 s?" Not `fraction_over` (that is the fraction of REQUESTS over x, with Wilson and evidence:
use it for that question). This is a time-weighted share, which only the histogram can give.
Setup: `query_distribution(selector)` -> d3. The threshold must sit on a bucket edge (never
split a bucket). The answer rests on bucket midpoints: the interval below covers sampling over
steps only, not the midpoint assumption, so the sensitivity range goes in `caveats`.

<!-- run: inputs=d3 show=out1 -->
```python
# expects d3 = a latency distribution dataset (counts per value bucket per step)
import numpy as np
import polars as pl
import telemetry_nerd.tn as tn


def acf_block(x):  # same helper as references/uncertainty.md
    n = x.size
    if n < 8:
        raise ValueError(f"{n} buckets: too few for a block bootstrap")
    xc = x - x.mean()
    acf = np.correlate(xc, xc, "full")[n - 1 :] / (xc @ xc)
    quiet = np.flatnonzero(np.abs(acf[1:]) < 2 / np.sqrt(n))
    length = quiet[0] + 1 if quiet.size else n // 4
    return int(min(max(length, round(n ** (1 / 3)), 2), n // 4))


X = 1.0  # seconds, in the dataset's unit: tn.meta("d3")["unit"]
df = tn.dataset("d3")
edges = set(df["bucket_lo"].to_list()) | set(df["bucket_hi"].to_list())
assert X in edges, f"threshold must be a bucket edge; edges: {sorted(edges)[:20]}"

# lowest bucket starts at -inf (latencies are >= 0); top bucket ends at +inf (use its lower edge)
lo_edge = pl.col("bucket_lo").clip(lower_bound=0.0)
hi_edge = pl.when(pl.col("bucket_hi").is_infinite()).then(lo_edge).otherwise(pl.col("bucket_hi"))
slow_bucket = pl.col("bucket_lo") >= X


def per_step(point):
    t = df.with_columns(wait=pl.col("count") * point)
    return (
        t.group_by("ts_ms")
        .agg(
            slow=pl.col("wait").filter(slow_bucket).sum(),
            total=pl.col("wait").sum(),
        )
        .sort("ts_ms")
    )


mid = (lo_edge + hi_edge) / 2
steps = per_step(mid)
slow, total = steps["slow"].to_numpy(), steps["total"].to_numpy()
assert np.isfinite(total).all() and total.sum() > 0, "empty or open-ended buckets: check the data"
share = slow.sum() / total.sum()
# sensitivity to the within-bucket assumption: every request at its bucket's lower / upper edge
bounds = []
for point in (lo_edge, hi_edge):
    s = per_step(point)
    bounds.append(s["slow"].sum() / s["total"].sum())

m = slow.size
block = acf_block(slow - share * total)
rng = np.random.default_rng(1)
starts = rng.integers(0, m - block + 1, size=(2000, -(-m // block)))
idx = (starts[:, :, None] + np.arange(block)).reshape(2000, -1)[:, :m]
boot = slow[idx].sum(axis=1) / total[idx].sum(axis=1)
lo, hi = np.quantile(boot, [0.025, 0.975])

ts = int(steps["ts_ms"].min())
span = int(steps["ts_ms"].max()) - ts + tn.meta("d3")["step_ms"]
out = pl.DataFrame({"ts_ms": [ts], "avg": [share], "lo": [lo], "hi": [hi]})
tn.put(
    out,
    representation="bucket_agg",  # not like="d3": a distribution's shape does not carry over
    step_ms=span,
    start_ms=ts,
    end_ms=ts + span,
    unit="ratio",
    description=f"share of latency time in requests >= {X}; interval = sampling over steps only",
    uncertainty={"method": f"moving-block bootstrap, block={block}", "level": 0.95},
    caveats=[
        f"bucket midpoints stand in for latencies; edge assumptions give {min(bounds):.3f} to "
        f"{max(bounds):.3f} (not in the interval); open top bucket counted at its lower edge",
        "the window is treated as one stationary sample",
    ],
)
print(f"share {share:.3f} [{lo:.3f}, {hi:.3f}], edge range {min(bounds):.3f}-{max(bounds):.3f}")
```

## C. Linear fit, autocorrelation-aware intervals, prediction band and time to full

Question: "at this growth rate, when does the volume fill?" Tier-1 `analyze` already reports a
trend with an autocorrelation-aware interval: use it for "is it growing and how fast". Use a fit
here because the answer needs a projection to a threshold. Setup: `query("disk_used_bytes")`
-> d4 (one series; reduce first with `query("sum(...)")` if there are several).

Cumulative gauges have strongly autocorrelated residuals, so plain OLS intervals are too narrow.
The example scales the parameter variance by (1+r)/(1-r) (AR(1) approximation, r = lag-1
autocorrelation of the residuals), uses a t quantile on n_eff-2 degrees of freedom, and records
the diagnostics. The time to full is where the confidence band of the trend line crosses the
capacity. A future prediction band still assumes the trend continues.

<!-- run: inputs=d4 show=fit1_prediction -->
```python
# expects d4 = one series, roughly linear growth
import numpy as np
import polars as pl
import telemetry_nerd.tn as tn

try:
    from scipy import stats  # in the [analysis] extra

    def tq(df):
        return float(stats.t.ppf(0.975, df))

    quantile = "t"
except ImportError:

    def tq(df):  # normal quantile: fine for df > 100; the caveat says so
        return 1.96

    quantile = "normal"


CAPACITY = 150.0  # in the dataset's unit; take it from the catalog bound or ask the user
HORIZON_H = 24  # prediction band shown this far past the data
d = tn.dataset("d4").sort("ts_ms")
t0 = int(d["ts_ms"][0])
x = (d["ts_ms"].to_numpy() - t0) / 3_600_000  # hours
y = d["avg"].to_numpy()
n = x.size
X = np.column_stack([np.ones(n), x])
beta, *_ = np.linalg.lstsq(X, y, rcond=None)
res = y - X @ beta
s2 = res @ res / (n - 2)
cov = s2 * np.linalg.inv(X.T @ X)

r = float(res[1:] @ res[:-1] / (res @ res))  # lag-1 autocorrelation of the residuals
infl = (1 + r) / (1 - r) if r > 0 else 1.0  # variance inflation for the parameters
n_eff = n / infl
t = tq(max(n_eff - 2, 1.0))
se = np.sqrt(infl * np.diag(cov))
dw = float(np.sum(np.diff(res) ** 2) / np.sum(res**2))  # ~2 means no lag-1 autocorrelation
r2 = 1 - float(res @ res / np.sum((y - y.mean()) ** 2))
z = res / res.std()
skew, kurt = float(np.mean(z**3)), float(np.mean(z**4) - 3)


def band(xs, prediction):
    """Half width: confidence band of the trend line, or the prediction band for new values."""
    q = np.einsum("ij,jk,ik->i", xs, infl * cov, xs)
    return t * np.sqrt(q + (s2 if prediction else 0.0))


def design(xs):
    return np.column_stack([np.ones(xs.size), xs])


xf = np.arange(x[-1], x[-1] + HORIZON_H + 1e-9, 0.25)
fit = design(xf) @ beta
half = band(design(xf), prediction=True)
pred = pl.DataFrame(
    {
        "ts_ms": (t0 + xf * 3_600_000).astype("int64"),
        "avg": fit,
        "lo": fit - half,
        "hi": fit + half,
    }
)

params = {
    "slope_per_hour": {
        "value": float(beta[1]),
        "interval": [float(beta[1] - t * se[1]), float(beta[1] + t * se[1])],
    },
    "intercept": {
        "value": float(beta[0]),
        "interval": [float(beta[0] - t * se[0]), float(beta[0] + t * se[0])],
    },
}
# time to full: first hour (from the last observation) where the trend line, and the upper and
# lower edge of its confidence band, reach CAPACITY (earliest = upper edge, latest = lower edge)
grid = np.arange(x[-1], x[-1] + 24 * 30, 0.25)
line, w = design(grid) @ beta, band(design(grid), prediction=False)


def crossing(curve):
    hit = np.flatnonzero(curve >= CAPACITY)
    return float(grid[hit[0]] - x[-1]) if hit.size else None


when = [crossing(line), crossing(line + w), crossing(line - w)]
if None not in when:
    params["hours_to_full"] = {"value": when[0], "interval": [when[1], when[2]]}
tn.put_fit(
    "linear",
    params,
    method="OLS with AR(1) variance inflation",
    level=0.95,
    diagnostics={
        "durbin_watson": dw,
        "lag1_autocorr": r,
        "n": n,
        "n_eff": n_eff,
        "resid_std": float(np.sqrt(s2)),
        "resid_skew": skew,
        "resid_excess_kurtosis": kurt,
    },
    goodness={"r2": r2},
    caveats=[
        "assumes growth stays linear; the AR(1) correction is approximate",
        f"{quantile} quantile, n_eff-2={n_eff - 2:.0f} degrees of freedom",
    ],
    prediction=pred,
    prediction_meta={
        "like": "d4",
        "start_ms": int(pred["ts_ms"].min()),
        "end_ms": int(pred["ts_ms"].max()),
        "step_ms": 900_000,
        "uncertainty": {"method": "OLS prediction interval", "level": 0.95, "kind": "prediction"},
    },
)
print(f"slope {beta[1]:.4g}/h, DW {dw:.2f}, r {r:.2f}, n_eff {n_eff:.0f}/{n}, to full {when[0]} h")
```
