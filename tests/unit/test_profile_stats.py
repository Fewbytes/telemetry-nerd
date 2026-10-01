"""T1 operating profile statistics on synthetic data with known parameters (bead 2as.7)."""

from __future__ import annotations

import math

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.analysis.profile import (
    HOUR_MS,
    band_at,
    compute_profile,
    hour_of_week,
)
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, labels_json, series_id

DAY = 24 * HOUR_MS
# 2026-09-07 00:00 UTC is a Monday
MONDAY = 1_788_739_200_000
SIGMA = 2.0


def truth(t_start_ms: np.ndarray, *, daily=20.0, weekend=-30.0, base=100.0) -> np.ndarray:
    hod = (t_start_ms // HOUR_MS) % 24
    dow = ((t_start_ms - MONDAY) // DAY) % 7
    return base + daily * np.sin(2 * np.pi * hod / 24) + weekend * (dow >= 5)


def table(values_by_series: dict[str, np.ndarray], start_ms: int, *, spread=5.0):
    """Hourly buckets: bucket ts is the END of its hour; avg = value, min/max = value -/+ spread."""
    rows = {"ts_ms": [], "series_id": [], "avg": [], "min": [], "max": [], "count": []}
    sids = []
    for inst, vals in values_by_series.items():
        sid = series_id("s", {"instance": inst})
        sids.append((sid, inst))
        for k, v in enumerate(vals):
            ok = v is not None and not (isinstance(v, float) and math.isnan(v))
            rows["ts_ms"].append(start_ms + (k + 1) * HOUR_MS)
            rows["series_id"].append(sid)
            rows["avg"].append(float(v) if ok else None)
            rows["min"].append(float(v) - spread if ok else None)
            rows["max"].append(float(v) + spread if ok else None)
            rows["count"].append(240)
    series = pa.table(
        {
            "series_id": [s for s, _ in sids],
            "labels": [labels_json({"instance": i}) for _, i in sids],
        },
        schema=SERIES_SCHEMA,
    )
    return pa.table(rows, schema=BUCKET_SCHEMA), series


def profile(values_by_series, *, days=30, start=MONDAY, extremes=True, kind="level"):
    buckets, series = table(values_by_series, start)
    return compute_profile(
        buckets,
        series,
        start_ms=start + HOUR_MS,
        end_ms=start + days * DAY,
        step_ms=HOUR_MS,
        extremes=extremes,
        kind=kind,
    )


def starts(days, start=MONDAY):
    return start + np.arange(days * 24) * HOUR_MS


def test_hour_of_week_is_utc_monday_zero():
    assert hour_of_week(MONDAY) == 0
    assert hour_of_week(MONDAY + 25 * HOUR_MS) == 25
    assert hour_of_week(MONDAY - HOUR_MS) == 167


def test_weekly_seasonal_profile_recovers_known_levels_and_band():
    rng = np.random.default_rng(1)
    t = starts(28)
    p = profile({"a": truth(t) + rng.normal(0, SIGMA, t.size)}, days=28)
    s = p.series[0]
    assert s.n == 28 * 24 and s.coverage == pytest.approx(1.0)
    seas = s.seasonal
    assert seas is not None and seas.period == "hour_of_week"
    assert seas.scores["hour_of_week"] < seas.scores["hour_of_day"] < seas.scores["none"]
    levels = np.array([b.level for b in seas.buckets])
    expected = truth(MONDAY + np.arange(168) * HOUR_MS)
    # median of 4 normal samples: sd ~ 1.2 sigma / 2; allow ~4 sd
    assert np.max(np.abs(levels - expected)) < 3 * SIGMA
    assert np.mean(np.abs(levels - expected)) < SIGMA
    # band is a prediction interval: ~90% of a fresh week falls inside it
    fresh_t = MONDAY + 28 * DAY + np.arange(168) * HOUR_MS
    fresh = truth(fresh_t) + rng.normal(0, SIGMA, 168)
    inside = [b.lo <= v <= b.hi for b, v in zip(seas.buckets, fresh, strict=True)]
    assert 0.8 <= np.mean(inside) <= 0.99
    assert seas.band_coverage == 0.9
    # band width ~ 2 * 1.645 sigma, widened by the median's own error, never collapsed
    width = np.median([b.hi - b.lo for b in seas.buckets])
    assert 2 * 1.645 * SIGMA * 0.9 < width < 2 * 1.645 * SIGMA * 1.8


def test_band_at_finds_the_bucket_of_any_timestamp():
    rng = np.random.default_rng(2)
    t = starts(28)
    seas = profile({"a": truth(t) + rng.normal(0, SIGMA, t.size)}, days=28).series[0].seasonal
    later = MONDAY + 70 * DAY + 13 * HOUR_MS + 5 * 60_000  # Monday 13:05, ten weeks on
    b = band_at(seas, later)
    assert b is seas.buckets[13]
    assert b.lo < b.level < b.hi


def test_daily_only_pattern_prefers_hour_of_day():
    rng = np.random.default_rng(3)
    t = starts(28)
    s = profile({"a": truth(t, weekend=0.0) + rng.normal(0, SIGMA, t.size)}, days=28).series[0]
    assert s.seasonal.period == "hour_of_day"
    assert len(s.seasonal.buckets) == 24
    levels = np.array([b.level for b in s.seasonal.buckets])
    expected = 100 + 20 * np.sin(2 * np.pi * np.arange(24) / 24)
    assert np.max(np.abs(levels - expected)) < 1.5 * SIGMA


def test_flat_noise_prefers_no_seasonality():
    rng = np.random.default_rng(4)
    s = profile({"a": 50 + rng.normal(0, SIGMA, 28 * 24)}, days=28).series[0]
    assert s.seasonal.period == "none"
    (b,) = s.seasonal.buckets
    assert b.level == pytest.approx(50, abs=0.5)
    assert b.hi - b.lo == pytest.approx(2 * 1.645 * SIGMA, rel=0.2)


def test_robust_range_and_envelope_are_the_known_quantiles():
    rng = np.random.default_rng(5)
    v = rng.normal(10, 1, 30 * 24)
    s = profile({"a": v}).series[0]
    r = s.range
    assert r.p005 == pytest.approx(np.quantile(v, 0.005))
    assert r.p995 == pytest.approx(np.quantile(v, 0.995))
    assert r.p50 == pytest.approx(np.median(v))
    assert r.p25 == pytest.approx(np.quantile(v, 0.25))
    assert r.mad == pytest.approx(np.median(np.abs(v - np.median(v))))
    # absolute extremes from intra-hour min/max, envelope from their robust tails
    assert r.min == pytest.approx(v.min() - 5) and r.max == pytest.approx(v.max() + 5)
    assert r.envelope_lo == pytest.approx(np.quantile(v - 5, 0.005))
    assert r.envelope_hi == pytest.approx(np.quantile(v + 5, 0.995))
    assert "low_n_tails" not in s.caveats


def test_one_outlier_does_not_move_the_robust_range():
    v = np.full(30 * 24, 10.0) + np.random.default_rng(6).normal(0, 0.1, 30 * 24)
    v[100] = 1e6
    r = profile({"a": v}).series[0].range
    assert r.max == pytest.approx(1e6 + 5)
    assert r.p995 < 11


def test_without_intra_hour_extremes_the_envelope_is_the_hourly_range():
    v = np.random.default_rng(7).normal(10, 1, 30 * 24)
    p = profile({"a": v}, extremes=False)
    r = p.series[0].range
    assert r.min == pytest.approx(v.min()) and r.max == pytest.approx(v.max())
    assert r.envelope_lo == r.p005 and r.envelope_hi == r.p995
    assert "no_intra_hour_extremes" in p.caveats


def test_short_history_is_reported_and_hour_of_week_is_not_faked():
    rng = np.random.default_rng(8)
    start = MONDAY + 20 * DAY  # only the last 10 days of the 30-day window have data
    t = starts(10, start)
    buckets, series = table({"a": truth(t) + rng.normal(0, SIGMA, t.size)}, start)
    p = compute_profile(
        buckets,
        series,
        start_ms=MONDAY + HOUR_MS,
        end_ms=MONDAY + 30 * DAY,
        step_ms=HOUR_MS,
        extremes=True,
        kind="level",
    )
    s = p.series[0]
    assert s.n == 240 and s.expected == 720
    assert s.coverage == pytest.approx(240 / 720)
    assert s.history_ms == 10 * DAY
    assert "short_history" in s.caveats and "gaps" not in s.caveats
    assert "low_n_tails" not in s.caveats  # 240 >= 200
    assert s.seasonal.period == "hour_of_day"  # ~1.4 samples per hour-of-week bucket
    assert s.seasonal.eligible == ["none", "hour_of_day"]


def test_two_days_only_allow_a_flat_profile_and_flag_tails():
    rng = np.random.default_rng(9)
    s = profile({"a": 5 + rng.normal(0, 1, 48)}, days=2).series[0]
    assert s.seasonal.period == "none"
    assert "low_n_tails" in s.caveats


def test_gaps_and_nan_hours_are_counted_not_filled():
    rng = np.random.default_rng(10)
    v = list(10 + rng.normal(0, 1, 30 * 24))
    for k in range(200, 400):
        v[k] = None  # a missing stretch inside the history
    v[500] = float("nan")
    p = profile({"a": v})
    s = p.series[0]
    assert s.n == 30 * 24 - 201
    assert "gaps" in s.caveats and "short_history" not in s.caveats


def test_sparse_hour_of_week_buckets_carry_no_level():
    rng = np.random.default_rng(11)
    t = starts(28)
    v = list(truth(t) + rng.normal(0, SIGMA, t.size))
    for week in range(3):  # Monday 05:00 has only one sample left
        v[week * 168 + 5] = None
    seas = profile({"a": v}, days=28).series[0].seasonal
    assert seas.period == "hour_of_week"
    assert seas.buckets[5].level is None and seas.buckets[5].n == 1
    assert seas.sparse_buckets == 1


def test_pooled_range_spans_all_series_and_seasonal_is_capped():
    rng = np.random.default_rng(12)
    data = {f"i{k}": 10 * (k + 1) + rng.normal(0, 1, 30 * 24) for k in range(14)}
    p = profile(data)
    assert p.series_total == 14 and len(p.series) == 14
    assert p.pooled.n == 14 * 30 * 24
    assert p.pooled.p005 < 11 and p.pooled.p995 > 139
    with_seasonal = [s for s in p.series if s.seasonal is not None]
    assert len(with_seasonal) == 12
    assert "seasonal_series_capped" in p.caveats


def test_a_rising_level_is_flagged_as_a_probable_counter():
    v = np.cumsum(np.random.default_rng(13).uniform(1, 2, 30 * 24))
    s = profile({"a": v}).series[0]
    assert "looks_like_counter" in s.caveats
    rates = profile({"a": np.diff(v, prepend=0)}, kind="rate").series[0]
    assert "looks_like_counter" not in rates.caveats


def test_no_data_gives_an_empty_profile_not_an_error():
    buckets, series = table({}, MONDAY)
    p = compute_profile(
        buckets,
        series,
        start_ms=MONDAY + HOUR_MS,
        end_ms=MONDAY + 30 * DAY,
        step_ms=HOUR_MS,
        extremes=True,
        kind="level",
    )
    assert p.series == [] and p.pooled.n == 0 and p.pooled.p50 is None
    assert "no_data" in p.caveats
