"""T1 operating profile statistics (spec §4.2; bead 2as.7). Pure: no I/O.

Input is hourly buckets (avg/min/max/count per series, bucket ts = END of its hour). Output is a
robust description of normal behaviour: range quantiles over hourly values, an envelope from the
intra-hour extremes, and a seasonal level + prediction band chosen by leave-one-out error.
Design notes: docs/superpowers/specs/2026-10-01-operating-profile-design.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

import numpy as np
import polars as pl
import pyarrow as pa
from pydantic import BaseModel

from telemetry_nerd.model.time import zone

HOUR_MS = 3_600_000
DAY_HOURS = 24
WEEK_HOURS = 168
#: 1970-01-01 was a Thursday; hour-of-week index 0 is Monday 00:00 (in the profile's timezone)
_EPOCH_HOW_OFFSET = 3 * DAY_HOURS

MIN_BUCKET_N = 3  # fewer samples per seasonal bucket: no level (never faked)
MIN_ELIGIBLE_SHARE = 0.9  # share of buckets that must reach MIN_BUCKET_N
RICHER_MODEL_GAIN = 0.98  # a richer seasonal model must cut LOO error by >= 2%
MIN_POOL_N = 20  # residual pool size per hour of day, else pool globally
TAIL_N = 200  # below this, p0.5 / p99.5 are essentially the extremes
COVERAGE_OK = 0.9
COUNTER_RISE_SHARE = 0.95
MAX_SEASONAL_SERIES = 12
BAND_COVERAGE = 0.9

Kind = Literal["level", "rate", "quantile"]
Period = Literal["none", "hour_of_day", "hour_of_week"]
_PERIODS: tuple[Period, ...] = ("none", "hour_of_day", "hour_of_week")
_PERIOD_LEN = {"none": 1, "hour_of_day": DAY_HOURS, "hour_of_week": WEEK_HOURS}


class RangeStats(BaseModel):
    """Quantiles (numpy type 7, linear) over hourly values; min/max/envelope from extremes."""

    n: int
    min: float | None = None
    p005: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p995: float | None = None
    max: float | None = None
    #: median absolute deviation from the median, unscaled
    mad: float | None = None
    #: [p0.5 of hourly minima, p99.5 of hourly maxima]: what a native-resolution view spans
    envelope_lo: float | None = None
    envelope_hi: float | None = None


class SeasonalBucket(BaseModel):
    i: int
    n: int
    level: float | None = None
    lo: float | None = None
    hi: float | None = None
    q25: float | None = None
    q75: float | None = None


class Seasonal(BaseModel):
    period: Period
    tz: str = "UTC"
    buckets: list[SeasonalBucket]
    #: central coverage of [lo, hi]
    band_coverage: float
    residual_pool: Literal["hour_of_day", "all"]
    residual_n: int
    #: leave-one-out mean absolute error per eligible model (lower is better)
    scores: dict[str, float]
    eligible: list[str]
    sparse_buckets: int
    amplitude: float | None = None


class SeriesProfile(BaseModel):
    series_id: str
    labels: dict[str, str]
    n: int
    expected: int
    coverage: float
    first_ms: int | None = None
    last_ms: int | None = None
    history_ms: int = 0
    range: RangeStats
    seasonal: Seasonal | None = None
    caveats: list[str] = []


class ProfileStats(BaseModel):
    kind: Kind
    #: timezone the seasonal hours are counted in (the source's: human load follows local time)
    tz: str = "UTC"
    step_ms: int
    start_ms: int  # first bucket ts (end of the first hour)
    end_ms: int  # last bucket ts
    expected: int  # buckets per series in the window
    extremes: bool  # min/max are true intra-step extremes
    pooled: RangeStats
    series: list[SeriesProfile]
    series_total: int
    caveats: list[str]


def utc_offset_ms(instant_ms: np.ndarray, tz: str) -> np.ndarray:
    """UTC offset of the timezone at each instant (DST-aware), in ms. Zero for UTC."""
    x = np.asarray(instant_ms, np.int64)
    if tz == "UTC":
        return np.zeros_like(x)
    z = zone(tz)
    # offsets change a few times a year: look each distinct hour up once
    hours, inverse = np.unique(x // HOUR_MS, return_inverse=True)
    offs = np.array(
        [
            int(datetime.fromtimestamp(h * 3600, UTC).astimezone(z).utcoffset().total_seconds())
            * 1000
            for h in hours
        ],
        np.int64,
    )
    return offs[inverse]


def local_hours(instant_ms: np.ndarray, tz: str) -> np.ndarray:
    """Whole hours since the epoch on the timezone's wall clock (floor: half-hour zones land
    their hour on the hour that contains it)."""
    x = np.asarray(instant_ms, np.int64)
    return (x + utc_offset_ms(x, tz)) // HOUR_MS


def hour_of_week(ts_ms: int, tz: str = "UTC") -> int:
    """Hour index of the hour containing ts, Monday 00:00 (local to `tz`) = 0."""
    return int((local_hours(np.array([ts_ms]), tz)[0] + _EPOCH_HOW_OFFSET) % WEEK_HOURS)


def _bucket_index(period: Period, hour_start_ms: np.ndarray, tz: str = "UTC") -> np.ndarray:
    hours = local_hours(hour_start_ms, tz)
    if period == "none":
        return np.zeros_like(hours)
    if period == "hour_of_day":
        return hours % DAY_HOURS
    return (hours + _EPOCH_HOW_OFFSET) % WEEK_HOURS


def band_at(seasonal: Seasonal, ts_ms: int) -> SeasonalBucket:
    """The seasonal bucket for the hour containing `ts_ms` (an instant, not a bucket end:
    callers holding bucket-end timestamps pass ts - step)."""
    i = int(_bucket_index(seasonal.period, np.array([ts_ms], dtype=np.int64), seasonal.tz)[0])
    return seasonal.buckets[i]


def seasonal_shape(seasonal: Seasonal, instant_ms: np.ndarray) -> np.ndarray:
    """The profile's seasonal curve at given instants, relative to its median level.

    Bucket levels (medians of hourly means) sit at their hour's midpoint and are joined
    linearly around the cycle; sparse buckets (no level) are bridged by their neighbours.
    NaN everywhere when no bucket has a level, zero for the `none` model."""
    x = np.asarray(instant_ms, np.int64)
    if seasonal.period == "none":
        return np.zeros(x.size)
    size = _PERIOD_LEN[seasonal.period]
    lv = np.array([np.nan if b.level is None else b.level for b in seasonal.buckets], float)
    ok = ~np.isnan(lv)
    if not ok.any():
        return np.full(x.size, np.nan)
    centres = np.flatnonzero(ok) + 0.5
    hours = (x + utc_offset_ms(x, seasonal.tz)) / HOUR_MS
    if seasonal.period == "hour_of_week":
        hours = hours + _EPOCH_HOW_OFFSET
    phase = np.mod(hours, size)
    out = np.interp(phase, centres, lv[ok], period=size)
    return out - float(np.median(lv[ok]))


# ---------------------------------------------------------------------------
def _q(x: np.ndarray, q: float) -> float:
    return float(np.quantile(x, q))


def range_stats(avg: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> RangeStats:
    if avg.size == 0:
        return RangeStats(n=0)
    med = float(np.median(avg))
    return RangeStats(
        n=int(avg.size),
        min=float(lo.min()),
        p005=_q(avg, 0.005),
        p25=_q(avg, 0.25),
        p50=med,
        p75=_q(avg, 0.75),
        p995=_q(avg, 0.995),
        max=float(hi.max()),
        mad=float(np.median(np.abs(avg - med))),
        envelope_lo=_q(lo, 0.005),
        envelope_hi=_q(hi, 0.995),
    )


def _loo_medians(x: np.ndarray) -> np.ndarray:
    """For each element, the median of all the OTHER elements (len >= 2)."""
    n = x.size
    order = np.argsort(x, kind="stable")
    s = x[order]
    k = np.arange(n)  # sorted position removed
    length = n - 1
    j1, j2 = (length - 1) // 2, length // 2
    med = (s[j1 + (j1 >= k)] + s[j2 + (j2 >= k)]) / 2
    out = np.empty(n)
    out[order] = med
    return out


def _fit(period: Period, idx: np.ndarray, x: np.ndarray):
    """Per-bucket counts and medians, and LOO residuals (NaN where the bucket is sparse)."""
    size = _PERIOD_LEN[period]
    counts = np.bincount(idx, minlength=size)
    levels = np.full(size, np.nan)
    resid = np.full(x.size, np.nan)
    for b in np.nonzero(counts)[0]:
        sel = idx == b
        xs = x[sel]
        if xs.size >= MIN_BUCKET_N:
            levels[b] = float(np.median(xs))
            resid[sel] = xs - _loo_medians(xs)
    return counts, levels, resid


def seasonal_profile(
    hour_start_ms: np.ndarray,
    x: np.ndarray,
    band_coverage: float = BAND_COVERAGE,
    tz: str = "UTC",
) -> Seasonal | None:
    if x.size < MIN_BUCKET_N:
        return None
    fits = {}
    for period in _PERIODS:
        idx = _bucket_index(period, hour_start_ms, tz)
        counts, levels, resid = _fit(period, idx, x)
        full = int(np.sum(counts >= MIN_BUCKET_N))
        if full >= MIN_ELIGIBLE_SHARE * _PERIOD_LEN[period]:
            fits[period] = (idx, counts, levels, resid)
    if not fits:
        return None
    # compare models on the same hours: those every eligible model can predict
    common = np.logical_and.reduce([~np.isnan(f[3]) for f in fits.values()])
    scores = {p: float(np.mean(np.abs(f[3][common]))) for p, f in fits.items()}
    chosen: Period = "none"
    for period in _PERIODS[1:]:
        if period in scores and scores[period] < scores[chosen] * RICHER_MODEL_GAIN:
            chosen = period
    idx, counts, levels, resid = fits[chosen]
    ok = ~np.isnan(resid)
    hod = _bucket_index("hour_of_day", hour_start_ms, tz)
    pools = [resid[ok & (hod == h)] for h in range(DAY_HOURS)]
    by_hod = chosen != "none" and all(p.size >= MIN_POOL_N for p in pools)
    a = (1 - band_coverage) / 2
    # Weibull (type 6) quantiles: [X(k), X(n+1-k)] covers a new draw with probability exactly
    # (n+1-2k)/(n+1); type 7 under-covers small pools (28 residuals: ~84% for a 90% band)
    probs = [a, 0.25, 0.75, 1 - a]
    global_q = np.quantile(resid[ok], probs, method="weibull")
    hod_q = [np.quantile(p, probs, method="weibull") for p in pools] if by_hod else None
    buckets = []
    for b in range(_PERIOD_LEN[chosen]):
        lvl = levels[b]
        if np.isnan(lvl):
            buckets.append(SeasonalBucket(i=b, n=int(counts[b])))
            continue
        if hod_q is not None:
            # hour of day of this bucket: any hour start in it will do
            h = b % DAY_HOURS if chosen != "none" else 0
            qs = hod_q[h]
        else:
            qs = global_q
        buckets.append(
            SeasonalBucket(
                i=b,
                n=int(counts[b]),
                level=float(lvl),
                lo=float(lvl + qs[0]),
                q25=float(lvl + qs[1]),
                q75=float(lvl + qs[2]),
                hi=float(lvl + qs[3]),
            )
        )
    finite = levels[~np.isnan(levels)]
    return Seasonal(
        period=chosen,
        tz=tz,
        buckets=buckets,
        band_coverage=band_coverage,
        residual_pool="hour_of_day" if by_hod else "all",
        residual_n=int(ok.sum()),
        scores=scores,
        eligible=list(fits),
        sparse_buckets=int(np.sum(counts < MIN_BUCKET_N)),
        amplitude=float(finite.max() - finite.min()) if finite.size else None,
    )


def _looks_like_counter(ts: np.ndarray, x: np.ndarray, step_ms: int) -> bool:
    adjacent = np.diff(ts) == step_ms
    if adjacent.sum() < DAY_HOURS:
        return False
    d = np.diff(x)[adjacent]
    return bool(np.mean(d > 0) > COUNTER_RISE_SHARE)


def compute_profile(
    buckets: pa.Table,
    series: pa.Table,
    *,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    extremes: bool,
    kind: Kind,
    max_seasonal_series: int = MAX_SEASONAL_SERIES,
    band_coverage: float = BAND_COVERAGE,
    tz: str = "UTC",
) -> ProfileStats:
    expected = (end_ms - start_ms) // step_ms + 1
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    df = df.filter(pl.col("ts_ms").is_between(start_ms, end_ms))
    in_data = df.filter(pl.col("count") > 0)
    valid = in_data.filter(pl.col("avg").is_not_null() & pl.col("avg").is_not_nan())
    non_finite = in_data.filter(pl.col("avg").is_nan()).height
    no_value = in_data.filter(pl.col("avg").is_null()).height
    if extremes:
        valid = valid.with_columns(
            pl.col("min").fill_nan(None).fill_null(pl.col("avg")).alias("min"),
            pl.col("max").fill_nan(None).fill_null(pl.col("avg")).alias("max"),
        )
    else:
        valid = valid.with_columns(pl.col("avg").alias("min"), pl.col("avg").alias("max"))
    valid = valid.sort("series_id", "ts_ms")
    labels = {
        sid: lab
        for sid, lab in zip(
            series.column("series_id").to_pylist(), series.column("labels").to_pylist(), strict=True
        )
    }
    caveats: list[str] = []
    if not extremes:
        caveats.append("no_intra_hour_extremes")
    if kind == "quantile":
        caveats.append("quantile_series")
    if non_finite:
        caveats.append("non_finite")
    if no_value:
        caveats.append("no_value")

    per: list[SeriesProfile] = []
    groups = {k[0]: g for k, g in valid.group_by("series_id", maintain_order=True)}
    for sid in sorted(groups, key=lambda s: (-groups[s].height, labels.get(s, ""))):
        g = groups[sid]
        ts = g["ts_ms"].to_numpy()
        avg = g["avg"].to_numpy()
        first, last = int(ts[0]), int(ts[-1])
        history = last - first + step_ms
        n = int(avg.size)
        sc: list[str] = []
        if history < COVERAGE_OK * expected * step_ms:
            sc.append("short_history")
        if n < COVERAGE_OK * (history // step_ms):
            sc.append("gaps")
        if n < TAIL_N:
            sc.append("low_n_tails")
        if kind == "level" and _looks_like_counter(ts, avg, step_ms):
            sc.append("looks_like_counter")
        per.append(
            SeriesProfile(
                series_id=sid,
                labels=json.loads(labels[sid]) if sid in labels else {},
                n=n,
                expected=expected,
                coverage=n / expected,
                first_ms=first,
                last_ms=last,
                history_ms=history,
                range=range_stats(avg, g["min"].to_numpy(), g["max"].to_numpy()),
                caveats=sc,
            )
        )
    for sp in per[:max_seasonal_series]:
        g = groups[sp.series_id]
        sp.seasonal = seasonal_profile(
            g["ts_ms"].to_numpy() - step_ms, g["avg"].to_numpy(), band_coverage, tz
        )
    if len(per) > max_seasonal_series:
        caveats.append("seasonal_series_capped")
    if not per:
        caveats.append("no_data")
    for sp in per:
        caveats.extend(c for c in sp.caveats if c not in caveats)
    per.sort(key=lambda s: labels.get(s.series_id, ""))
    pooled = range_stats(valid["avg"].to_numpy(), valid["min"].to_numpy(), valid["max"].to_numpy())
    return ProfileStats(
        kind=kind,
        tz=tz,
        step_ms=step_ms,
        start_ms=start_ms,
        end_ms=end_ms,
        expected=expected,
        extremes=extremes,
        pooled=pooled,
        series=per,
        series_total=len(per),
        caveats=caveats,
    )
