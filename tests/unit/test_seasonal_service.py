"""compare_seasonal end to end over a fake source with a daily + weekly load (bead lkn.2)."""

import asyncio

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from tests.unit.fakes import FakeSource, make_service
from tests.unit.test_seasonal import DAY, MON, H, load

SAT = MON + 5 * DAY + 8 * H


class SeasonalSource(FakeSource):
    """One series of the synthetic weekly load; `boost` multiplies values in [a, b);
    no data before `history_from`."""

    def __init__(self, boost=None, history_from=None):
        super().__init__(n_series=1)
        self.boost = boost
        self.history_from = history_from

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        ts = np.arange(rng.start_ms, rng.end_ms + 1, step_ms)
        if self.history_from is not None:
            ts = ts[ts >= self.history_from]
        y = load(ts, np.random.default_rng(rng.start_ms // step_ms), 1.0)
        if self.boost:
            a, b, f = self.boost
            y = np.where((ts >= a) & (ts < b), y * f, y)
        sid = series_id(self.name, {"job": "api"})
        n = ts.size
        buckets = pa.table(
            {"ts_ms": ts, "series_id": [sid] * n, "avg": y, "min": y, "max": y, "count": [4] * n},
            schema=BUCKET_SCHEMA,
        )
        series = pa.table({"series_id": [sid], "labels": [labels_json({"job": "api"})]}, schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)


def window(svc, start, hours=12, step="5m", expr="sum(rate(http_requests_total[5m]))"):
    out = asyncio.run(svc.query(expr, start=str(start), end=str(start + hours * H), step=step))
    return out["dataset"]


def test_weekday_peak_on_saturday_is_unusual_against_last_saturdays(tmp_path):
    src = SeasonalSource(boost=None)
    svc = make_service(tmp_path, source=src)
    # make Saturday look like a weekday: boost the business-hours window x2.5
    src.boost = (SAT + 4 * H, SAT + 8 * H, 2.5)
    d = window(svc, SAT)
    out = asyncio.run(svc.compare_seasonal(d))
    (s,) = out["series"]
    assert s["verdict"] == "unusual" and s["direction"] == "higher", s["reasons"]
    ref = s["reference"]
    assert ref["scheme"] == "1w" and len(ref["cycles"]) == 4
    assert "previous 4 weeks, aligned by UTC" in ref["label"]
    assert s["ratio"]["evidence"]["name"] == "seasonal_ratio"
    assert out["alignment"].startswith("UTC") and out["draw"].endswith('mark="seasonal")')
    assert len(str(out)) < 4000


def test_normal_saturday_is_usual_and_panel_draws_cycles_and_band(tmp_path):
    svc = make_service(tmp_path, source=SeasonalSource())
    d = window(svc, SAT)
    out = asyncio.run(svc.compare_seasonal(d, cycles=["1w"]))
    (s,) = out["series"]
    assert s["verdict"] == "usual", s["reasons"]
    assert out["schemes"] == {"1w": "4 cycles fetched"}
    shown = svc.show(d, "Is this Saturday unusual for a Saturday?", mark="seasonal")
    data = svc.panel_data(shown.panel.id, 800)
    assert data["kind"] == "seasonal"
    (ps,) = data["series"]
    assert len(ps["cycles"]) == 4 and len(ps["now"]) == len(ps["ts"]) == len(ps["lo"])
    assert ps["ratio"]["kind"] == "ratio" and ps["scheme"] == "1w"
    inside = [lo <= v <= hi for v, lo, hi in zip(ps["now"], ps["lo"], ps["hi"], strict=True)]
    assert 0.75 < sum(inside) / len(inside) <= 1.0


def test_short_history_is_insufficient(tmp_path):
    svc = make_service(tmp_path, source=SeasonalSource(history_from=SAT - 15 * DAY))
    d = window(svc, SAT)
    out = asyncio.run(svc.compare_seasonal(d, cycles=["1w"]))
    (s,) = out["series"]
    assert s["verdict"] == "insufficient_history"
    assert [e["reason"] for e in s["reference"]["excluded"]] == ["missing", "missing"]


def test_user_excluded_holiday_is_stated(tmp_path):
    svc = make_service(tmp_path, source=SeasonalSource())
    d = window(svc, SAT)
    out = asyncio.run(svc.compare_seasonal(d, cycles=["1w"], exclude=["2026-09-26"]))
    (s,) = out["series"]
    assert s["reference"]["excluded"] == [
        {"j": 1, "start": "2026-09-26T08:00:00+00:00", "shift_h": 168.0, "reason": "user"}
    ]


def test_local_time_alignment_is_stated(tmp_path):
    svc = make_service(tmp_path, source=SeasonalSource())
    d = window(svc, SAT, hours=4)
    out = asyncio.run(svc.compare_seasonal(d, cycles=["1d"], tz="Europe/Berlin"))
    assert "local time Europe/Berlin" in out["alignment"]
    assert "aligned by local time Europe/Berlin" in out["series"][0]["reference"]["label"]


def test_refusals(tmp_path):
    svc = make_service(tmp_path, source=SeasonalSource())
    q = window(svc, SAT, expr="histogram_quantile(0.99, sum(rate(x_bucket[5m])) by (le))")
    with pytest.raises(ValueError, match="percentile.*hint: compare the histogram per cycle"):
        asyncio.run(svc.compare_seasonal(q))
    d = window(svc, SAT, hours=36)
    with pytest.raises(ValueError, match="no cycle applies"):
        asyncio.run(svc.compare_seasonal(d, cycles=["1d"]))
    with pytest.raises(ValueError, match="compare_seasonal"):
        svc.show(window(svc, SAT, hours=2), "q?", mark="seasonal")
    with pytest.raises(ValueError, match="timezone"):
        asyncio.run(svc.compare_seasonal(d, tz="Nowhere/Land"))


def test_spectrum_and_analyze_suggest_seasonal_compare_for_a_daily_cycle(tmp_path):
    svc = make_service(tmp_path, source=SeasonalSource())
    d = window(svc, MON, hours=24 * 6, step="30m")
    assert "compare_seasonal" in svc.spectrum(d).get("suggest", "")
    assert "compare_seasonal" in svc.analyze(d).get("suggest", "")
    assert svc.seasonal_suggestion(d) is None  # no operating profile cached


def test_show_suggests_seasonal_compare_when_the_profile_is_seasonal(tmp_path):
    svc = make_service(tmp_path, source=SeasonalSource())
    d = window(svc, SAT, hours=2)
    svc._profile_periods = lambda source, expr: ["hour_of_week"]
    assert "time of week" in svc.seasonal_suggestion(d)
    assert svc.seasonal_suggestion(d, mark="spc") is None


class LocalDailySource(SeasonalSource):
    """A daily load that follows Berlin wall-clock time (no weekly pattern)."""

    async def fetch(self, expr, rng, step_ms):
        from datetime import UTC, datetime
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("Europe/Berlin")
        self.calls += 1
        ts = np.arange(rng.start_ms, rng.end_ms + 1, step_ms)
        off = np.array([datetime.fromtimestamp(t / 1000, UTC).astimezone(zone).utcoffset().total_seconds() * 1000 for t in ts])  # fmt: skip
        hour = ((ts + off) % DAY) / H
        noise = np.random.default_rng(rng.start_ms // step_ms).normal(0, 0.03, ts.size)
        y = 100 * (1 + 3.0 * np.exp(-(((hour - 10) / 1.5) ** 2))) * np.exp(noise)
        sid = series_id(self.name, {"job": "api"})
        n = ts.size
        buckets = pa.table(
            {"ts_ms": ts, "series_id": [sid] * n, "avg": y, "min": y, "max": y, "count": [4] * n},
            schema=BUCKET_SCHEMA,
        )
        series = pa.table({"series_id": [sid], "labels": [labels_json({"job": "api"})]}, schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)


def test_window_across_a_dst_change_is_matched_by_wall_clock(tmp_path):
    # Sun 2026-03-29 00:00-12:00 UTC: Berlin springs forward at 01:00 UTC; the 10:00 local
    # peak is at 08:00 UTC today but 09:00 UTC on the previous days
    from datetime import UTC, datetime

    start = int(datetime(2026, 3, 29, tzinfo=UTC).timestamp() * 1000)
    svc = make_service(tmp_path, source=LocalDailySource())
    d = window(svc, start, hours=12)
    out = asyncio.run(svc.compare_seasonal(d, cycles=["1d"], tz="Europe/Berlin"))
    (s,) = out["series"]
    assert "dst_wall_clock" in out["caveats"] and "dst_within_window" not in out["caveats"]
    assert s["verdict"] == "usual", s["reasons"]
    assert s["reference"]["dst_shifted"] == list(range(1, 8))
    data = svc.panel_data(svc.show(d, "q?", mark="seasonal").panel.id, 800)
    (ps,) = data["series"]
    ratio = np.array([v for v in ps["ratio"]["value"] if v is not None])
    assert np.max(np.abs(np.log(ratio))) < 0.2  # an hour off would put the peak at x2.5
