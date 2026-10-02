"""Seasonal operating profile in local time (bead 2as.24)."""

import numpy as np
import pytest
from pydantic import ValidationError

from telemetry_nerd.analysis.profile import (
    HOUR_MS,
    band_at,
    hour_of_week,
    local_hours,
    seasonal_profile,
    seasonal_shape,
    utc_offset_ms,
)
from telemetry_nerd.model.time import check_timezone
from telemetry_nerd.sources.spec import SourceSpec

BERLIN = "Europe/Berlin"
# 2026-09-07 00:00 UTC is a Monday; Berlin leaves summer time at 01:00 UTC on 2026-10-25
START = 1_788_739_200_000
DST_END = 1_792_890_000_000


def hours(n: int) -> np.ndarray:
    return START + np.arange(n, dtype=np.int64) * HOUR_MS


def test_offsets_follow_dst():
    summer, winter = hours(1), np.array([DST_END + 5 * HOUR_MS])
    assert utc_offset_ms(summer, BERLIN)[0] == 2 * HOUR_MS
    assert utc_offset_ms(winter, BERLIN)[0] == 1 * HOUR_MS
    assert utc_offset_ms(summer, "UTC")[0] == 0
    around = np.array([DST_END - HOUR_MS, DST_END])
    assert list(utc_offset_ms(around, BERLIN)) == [2 * HOUR_MS, 1 * HOUR_MS]


def test_local_hour_of_week_and_half_hour_zones():
    assert hour_of_week(START) == 0  # Monday 00:00 UTC
    assert hour_of_week(START, BERLIN) == 2  # Monday 02:00 in Berlin
    assert hour_of_week(START, "America/New_York") == 168 - 4  # Sunday 20:00
    # India is +05:30: the local hour is the one that contains the instant
    assert int(local_hours(np.array([START]), "Asia/Kolkata")[0] % 24) == 5


def test_unknown_timezone_is_a_clear_error():
    assert check_timezone("Europe/Berlin") == "Europe/Berlin"
    with pytest.raises(ValueError, match="unknown timezone"):
        check_timezone("Mars/Olympus")
    with pytest.raises(ValidationError, match="unknown timezone"):
        SourceSpec(name="x", url="http://x", timezone="Mars/Olympus")
    assert SourceSpec(name="x", url="http://x").timezone == "UTC"


def office_load(ts: np.ndarray, tz: str = BERLIN) -> np.ndarray:
    """Human load: busy 08:00-17:59 local on weekdays, quiet otherwise; a little noise."""
    local = local_hours(ts, tz)
    how = (local + 72) % 168
    hod = how % 24
    busy = (how < 120) & (hod >= 8) & (hod < 18)
    noise = np.random.default_rng(7).normal(0, 0.5, ts.size)
    return 10.0 + 20.0 * busy + noise


def widest(s) -> float:
    """The widest band of any hour: where the shifted hours land in UTC buckets."""
    return max(b.hi - b.lo for b in s.buckets if b.level is not None)


def test_local_buckets_hold_a_band_across_a_dst_change():
    ts = hours(24 * 7 * 12)  # 12 weeks: 7 in summer time, 5 in winter time
    x = office_load(ts)
    utc = seasonal_profile(ts, x)
    local = seasonal_profile(ts, x, tz=BERLIN)
    assert utc is not None and local is not None
    assert (utc.tz, local.tz) == ("UTC", BERLIN)
    assert local.period == "hour_of_week"
    # UTC buckets at the office-hour edges mix summer and winter days (an hour apart): the band
    # there spans the whole 20-unit swing. Local buckets never see that shift.
    assert widest(utc) > 15
    assert widest(local) < 4
    assert local.scores["hour_of_week"] < utc.scores["hour_of_week"] / 1.8


def test_band_at_and_shape_read_the_profile_timezone():
    ts = hours(24 * 7 * 12)
    local = seasonal_profile(ts, office_load(ts), tz=BERLIN)
    assert local is not None
    summer_noon = START + 2 * 24 * HOUR_MS + 10 * HOUR_MS  # Wednesday 12:00 Berlin (UTC+2)
    winter_noon = DST_END + 3 * 24 * HOUR_MS + 10 * HOUR_MS  # Wednesday 12:00 Berlin (UTC+1)
    assert band_at(local, summer_noon) is band_at(local, winter_noon)
    assert band_at(local, summer_noon).level == pytest.approx(30, abs=1)
    night = START + 2 * 24 * HOUR_MS + 1 * HOUR_MS  # Wednesday 03:00 Berlin
    assert band_at(local, night).level == pytest.approx(10, abs=1)
    shape = seasonal_shape(local, np.array([summer_noon, winter_noon, night]))
    assert shape[0] == pytest.approx(shape[1], abs=0.5) and shape[0] > shape[2] + 15


# --- service: the source's timezone ---------------------------------------------------------
async def test_the_source_timezone_drives_the_profile_and_changing_it_recomputes(tmp_path):
    from .test_profiles import DISCOVERY, Clock, SeasonalSource, make_service

    clock = Clock(START + 8 * 7 * 24 * HOUR_MS)
    src = SeasonalSource(clock, name="default", discovery=DISCOVERY)
    svc = make_service(tmp_path, src, clock=clock)
    await svc.learn("default")
    first = await svc.profiles.ensure("default", "queue_depth")
    assert first.tz == "UTC" and first.series[0].seasonal.tz == "UTC"
    assert first.summary()["timezone"] == "UTC"
    assert svc.profiles.cached("default", "queue_depth").stale is False

    svc.sources.attach(
        "default", src, SourceSpec(name="default", url="http://main", timezone=BERLIN)
    )
    assert svc.profiles.cached("default", "queue_depth").stale is True  # wrong clock for the source
    again = await svc.profiles.ensure("default", "queue_depth")
    assert again.tz == BERLIN and again.series[0].seasonal.tz == BERLIN
    assert svc.profiles.cached("default", "queue_depth").stale is False
