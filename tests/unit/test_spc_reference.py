"""analyze against a separately fetched baseline (previous / day / week) and the seasonal
residual chart from the operating profile, end to end over a fake source (bead lkn.5)."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.series import series_id as sid_of
from tests.unit.fakes import FakeSource, make_service

M, H, DAY = 60_000, 3_600_000, 86_400_000
MONDAY = 1_788_739_200_000  # 2026-09-07 00:00 UTC
NOW = MONDAY + 40 * DAY + 30 * M
W0 = MONDAY + 37 * DAY + 6 * H  # a 6 h window on the falling flank of the daily cycle


class DailySource(FakeSource):
    """A gauge on a smooth daily cycle (amplitude 10, white noise sigma 1), evaluated at each
    bucket's midpoint; `boost` = (a, b, add) adds a level in [a, b)."""

    def __init__(self, boost=None):
        super().__init__(name="default", n_series=1)
        self.boost = boost

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        ts = np.arange(rng.start_ms, rng.end_ms + 1, step_ms)
        ts = ts[ts <= NOW]
        mid = ts - step_ms // 2
        noise = np.random.default_rng([rng.start_ms // M, step_ms // M]).normal(size=ts.size)
        y = 50 + 10 * np.sin(2 * np.pi * (mid % DAY) / DAY) + noise / np.sqrt(step_ms // M)
        if self.boost:
            a, b, add = self.boost
            y = np.where((ts > a) & (ts <= b), y + add, y)
        sid = sid_of(self.name, {"job": "api"})
        n = ts.size
        buckets = pa.table(
            {"ts_ms": ts, "series_id": [sid] * n, "avg": y, "min": y, "max": y, "count": [4] * n},
            schema=BUCKET_SCHEMA,
        )
        series = pa.table({"series_id": [sid], "labels": [labels_json({"job": "api"})]}, schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)

    fetch_values = fetch


def window(svc, start=W0, hours=6):
    out = asyncio.run(
        svc.query("queue_depth", start=str(start), end=str(start + hours * H), step="1m")
    )
    return out["dataset"]


@pytest.fixture
def svc(tmp_path):
    return make_service(tmp_path, source=DailySource(), clock=lambda: NOW)


def test_previous_window_baseline_is_fetched_stated_and_never_judged(svc):
    d = window(svc)
    out = asyncio.run(svc.analyze_reference(d, "previous"))
    b = out["baseline"]
    assert b["kind"] == "reference" and b["basis"].startswith("the preceding 361m window")
    (w,) = b["windows"]
    assert w["end"] == "2026-10-14T06:00:00+00:00" and w["dataset"] != d
    (s,) = out["series"]
    # no operating profile yet: a flat centre from the preceding window (another part of the
    # daily cycle) is not a usable chart, and says so
    assert s["spc"]["mode"] == "insufficient_data" and "n_eff" in s["spc"]["reason"]
    assert "uses this reference baseline" in out["draw"]


def test_profile_shape_makes_a_seasonal_residual_chart(svc):
    d = window(svc)
    asyncio.run(svc.profiles.ensure("default", "queue_depth"))
    out = asyncio.run(svc.analyze_reference(d, "previous"))
    spc = out["series"][0]["spc"]
    assert spc["centre"]["seasonal"].endswith("profile"), spc
    prof = spc["centre"]["seasonal_profile"]
    assert prof["model"] in ("hour_of_day", "hour_of_week") and prof["history"] == "30d"
    assert prof["judged_hours_excluded"] == 7  # the 6 h window touches 7 profile hours
    assert spc["in_control"] is True, spc
    # every point of the dataset is judged; the baseline is the separate fetch (361 points)
    assert spc["baseline"]["n"] == 361 and spc["detectors"]["ewma"]["of"] == 361
    assert "decays" in spc["settings"]["gaps"]
    assert "operating profile" in spc["centre"]["evidence"]["method"]
    # the default (in-dataset) baseline gets the same seasonal centre (3 h < 2 days)
    within = svc.analyze(d)["series"][0]["spc"]
    assert within["centre"]["seasonal"].endswith("profile") and within["in_control"] is True


def test_week_baseline_flags_a_shift_and_the_panel_carries_limit_uncertainty(tmp_path):
    src = DailySource(boost=(W0 + 4 * H, W0 + 6 * H, 2.5))
    svc = make_service(tmp_path, source=src, clock=lambda: NOW)
    d = window(svc)
    asyncio.run(svc.profiles.ensure("default", "queue_depth"))
    out = asyncio.run(svc.analyze_reference(d, "week", tz="Europe/Berlin"))
    assert "previous week, aligned by local time Europe/Berlin" in out["baseline"]["basis"]
    spc = out["series"][0]["spc"]
    assert spc["in_control"] is False and spc["detectors"]["cusum"]["p"] < 1e-6
    p = svc.show(d, "Higher than last week?", mark="spc").panel
    layer = p.spec["layers"][0]
    assert layer["spc"]["baseline"] == "week" and layer["windows"] == []
    data = svc.panel_data(p.id, 800)
    assert data["baseline"]["kind"] == "reference"
    (s,) = data["series"]
    lo, hi = s["centre_interval"]
    assert lo < s["level"] < hi and s["sigma_interval"][0] < s["sigma"] < s["sigma_interval"][1]
    assert s["seasonal"].endswith("profile") and s["seasonal_profile"]["model"]
    outside = [v["ts"] for v in s["violations"] if "outside_limits" in v["rules"]]
    assert len(outside) > 20 and np.mean(np.array(outside) > W0 + 4 * H) > 0.9
    assert len(json.dumps(data)) < 120_000


def test_profile_shape_never_sees_the_judged_hours(tmp_path):
    """A huge excursion in the judged window is in the cached profile's history; the centre
    must not follow it (the shape is re-fitted without those hours)."""
    src = DailySource(boost=(W0, W0 + 6 * H, 40.0))
    svc = make_service(tmp_path, source=src, clock=lambda: NOW)
    d = window(svc)
    asyncio.run(svc.profiles.ensure("default", "queue_depth"))
    data = svc.panel_data(svc.show(d, "?", mark="spc").panel.id, 800)
    (s,) = data["series"]
    # the in-dataset baseline is boosted too, so level absorbs the +40; the SHAPE must be
    # the daily cycle's, not one bent by the boosted hours
    ts = np.array(s["ts"])
    truth = 50 + 10 * np.sin(2 * np.pi * ((ts - M // 2) % DAY) / DAY)
    centre = np.array(s["centre"], float)
    resid = centre - truth
    assert np.ptp(resid) < 1.5, np.ptp(resid)


def test_reference_baselines_are_validated(svc):
    d = window(svc, hours=30)
    with pytest.raises(ValueError, match="longer than the 1d cycle"):
        asyncio.run(svc.analyze_reference(d, "day"))
    with pytest.raises(ValueError, match="unknown baseline"):
        asyncio.run(svc.analyze_reference(d, "month"))
    with pytest.raises(ValueError, match="1..4"):
        asyncio.run(svc.analyze_reference(d, "week", cycles=9))


def test_longer_baseline_from_several_previous_days(svc):
    d = window(svc)
    asyncio.run(svc.profiles.ensure("default", "queue_depth"))
    out = asyncio.run(svc.analyze_reference(d, "day", cycles=3))
    assert len(out["baseline"]["windows"]) == 3
    spc = out["series"][0]["spc"]
    assert spc["baseline"]["n"] == 3 * 361
    # 3 x 6 h of the same phase span 2+ days but observe only 18 h: still < 2 daily cycles,
    # so the profile's shape is the seasonal centre
    assert spc["centre"]["seasonal"].endswith("profile") and spc["in_control"] is True, spc


async def test_mcp_analyze_with_reference_baseline(tmp_path):
    from telemetry_nerd.mcp.server import build_mcp
    from tests.unit.test_mcp import call, text_of

    svc = make_service(tmp_path, source=DailySource(), clock=lambda: NOW)
    mcp = build_mcp(svc, "http://127.0.0.1:7070")
    q = await svc.query("queue_depth", start=str(W0), end=str(W0 + 6 * H), step="1m")
    d = q["dataset"]
    out = json.loads(text_of(await call(mcp, "analyze", {"dataset": d, "baseline": "day"})))
    assert out["baseline"]["kind"] == "reference"
    bad = await call(
        mcp, "analyze", {"dataset": d, "baseline": "week", "baseline_start": "2026-10-14T06:00:00Z"}
    )
    assert bad.is_error and "drop baseline_start" in text_of(bad)
