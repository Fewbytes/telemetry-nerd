"""A marginal against the operating profile (bead 2as.22)."""

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.analysis.profile import HOUR_MS
from telemetry_nerd.analysis.profile_reference import (
    ProfileReferenceRefused,
    describe,
    hourly_means,
    matched_hours,
)
from telemetry_nerd.charts.spec import ChartSpec
from telemetry_nerd.model.series import BUCKET_SCHEMA

from .fakes import make_service
from .test_profiles import DISCOVERY, MONDAY, NOW, Clock, SeasonalSource

M5 = 300_000


def table(rows):
    cols = list(zip(*rows, strict=True))
    return pa.table(
        {
            "ts_ms": list(cols[0]),
            "series_id": list(cols[1]),
            "avg": list(cols[2]),
            "min": list(cols[2]),
            "max": list(cols[2]),
            "count": list(cols[3]),
        },
        schema=BUCKET_SCHEMA,
    )


def test_hourly_means_are_count_weighted_and_whole_hours_only():
    # bucket ts is the END of its 5m bucket: hour 0 = 12 buckets ending 5m..60m; hour 1 only has 3
    rows = [(MONDAY + (i + 1) * M5, "a", 10.0 if i < 6 else 20.0, 1) for i in range(12)]
    rows[0] = (rows[0][0], "a", 22.0, 6)  # weight 6: (22*6 + 10*5 + 20*6) / 17
    rows += [(MONDAY + HOUR_MS + (i + 1) * M5, "a", 99.0, 1) for i in range(3)]
    df, dropped = hourly_means(table(rows), M5)
    assert df.height == 1 and dropped == 3
    assert df["hour_ms"][0] == MONDAY
    assert df["avg"][0] == pytest.approx((22 * 6 + 10 * 5 + 20 * 6) / 17)


def test_a_bucket_kept_without_samples_completes_its_hour_but_weighs_nothing():
    # uup: a spilled scrape's bucket is kept with count 0; like every count-weighted mean it
    # weighs 0, and it no longer leaves its hour incomplete
    rows = [(MONDAY + (i + 1) * M5, "a", 10.0, 1) for i in range(12)]
    rows[3] = (rows[3][0], "a", 1000.0, 0)
    df, dropped = hourly_means(table(rows), M5)
    assert df.height == 1 and dropped == 0
    assert df["avg"][0] == pytest.approx(10.0)
    # an hour of nothing but such buckets has no mean
    df, _ = hourly_means(table([(r[0], "a", 5.0, 0) for r in rows]), M5)
    assert df.height == 0


def test_a_step_that_cannot_roll_up_to_hours_is_refused():
    with pytest.raises(ProfileReferenceRefused, match="coarser than the profile's hour"):
        hourly_means(table([(MONDAY, "a", 1.0, 1)]), 2 * HOUR_MS)
    with pytest.raises(ProfileReferenceRefused, match="does not divide an hour"):
        hourly_means(table([(MONDAY, "a", 1.0, 1)]), 7 * 60_000)


def test_matching_keeps_only_the_buckets_the_window_covers():
    ref = MONDAY + np.arange(24 * 14, dtype=np.int64) * HOUR_MS  # two weeks of hours
    now = MONDAY + 9 * 24 * HOUR_MS + np.arange(6, dtype=np.int64) * HOUR_MS  # Wed 00:00-05:00
    assert matched_hours(now, ref, "none", "UTC").all()
    assert matched_hours(now, ref, "hour_of_day", "UTC").sum() == 6 * 14
    how = matched_hours(now, ref, "hour_of_week", "UTC")
    assert how.sum() == 6 * 2  # the same six hours of the week, in each of the two weeks
    assert matched_hours(now[:0], ref, "hour_of_week", "UTC").all()


def test_the_label_says_what_the_reference_is():
    assert describe("hour_of_week", 30, "UTC", None) == (
        "normal profile (30d hourly, same hours of the week, UTC)"
    )
    assert "no seasonal pattern" in describe("none", 30, "UTC", None)
    assert describe("hour_of_day", 30, "Europe/Berlin", "4h").endswith(
        "Europe/Berlin; rates over 4h)"
    )


@pytest.fixture
async def svc(tmp_path):
    clock = Clock(NOW)
    src = SeasonalSource(clock, name="default", discovery=DISCOVERY)
    service = make_service(tmp_path, src, clock=clock)
    await service.learn("default")
    return service


# --- through the service ---------------------------------------------------------------------
async def panel(svc, expr, **q):
    ds = await svc.query(expr, start=q.get("start", "now-6h"), end="now", step=q.get("step", "5m"))
    return svc.show(ds["dataset"], "Is now normal?", raw_ok=True).panel.id


async def test_the_window_is_compared_with_the_same_hours_of_the_profile(svc):
    pid = await panel(svc, "queue_depth")
    out = await svc.set_marginal(pid, "profile", "user", reason="is this a normal night?")
    assert out["basis"] == "samples" and "hourly means" in out["what"]
    assert out["reference"].startswith("normal profile (30d hourly, same hours of the week, UTC)")
    # ~6 whole hours now; the profile's matching hours: six hour-of-week slots over ~4.3 weeks
    assert 5 <= out["n"]["now"] <= 6
    assert 20 <= out["n"]["reference"] <= 36
    spec = ChartSpec.model_validate(svc.workspace.get_panel(pid).spec)
    assert spec.marginal.reference == "profile" and "profile" in spec.references
    ref_meta = svc.datasets.meta(spec.references["profile"].series)
    assert ref_meta.derived["op"] == "profile_reference" and ref_meta.step_ms == HOUR_MS


async def test_the_profile_marginal_has_hourly_windows_in_the_payload(svc):
    pid = await panel(svc, "queue_depth")
    await svc.set_marginal(pid, "profile", "user")
    data = svc.panel_data(pid, 800)
    wins = data["marginal"]["windows"]
    assert wins[0]["label"] == "now (hourly)" and wins[1]["label"].startswith("normal profile")
    assert data["marginal"]["reference"]["mode"] == "profile"
    assert sum(wins[0]["c"]) == pytest.approx(wins[0]["n"])


async def test_a_step_coarser_than_an_hour_is_refused_with_the_reason(svc):
    pid = await panel(svc, "queue_depth", start="now-4d", step="2h")
    with pytest.raises(ValueError, match="coarser than the profile's hour"):
        await svc.set_marginal(pid, "profile", "user")


async def test_percentile_panels_are_refused_not_averaged(svc):
    expr = "histogram_quantile(0.99, sum by (le) (rate(lat_seconds_bucket[5m])))"
    pid = await panel(svc, expr)
    with pytest.raises(ValueError, match="plain series"):
        await svc.set_marginal(pid, "profile", "user")
