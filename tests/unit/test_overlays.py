import json
from types import SimpleNamespace

import pytest
from mcp import Client
from starlette.testclient import TestClient

from telemetry_nerd.analysis.profile import (
    RangeStats,
    Seasonal,
    SeasonalBucket,
    hour_of_week,
)
from telemetry_nerd.api.app import create_app
from telemetry_nerd.charts.spec import ChartSpec, YContext
from telemetry_nerd.core.panel_payloads import limit_payload, normal_payload
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import FakeSource, make_service

HOUR = 3_600_000
STEP = 60_000


def seasonal(period="hour_of_week", n=168):
    return Seasonal(
        period=period,
        buckets=[
            SeasonalBucket(i=i, n=5, level=i + 0.5, lo=float(i), hi=float(i + 1)) for i in range(n)
        ],
        band_coverage=0.9,
        residual_pool="all",
        residual_n=10,
        scores={},
        eligible=[],
        sparse_buckets=0,
    )


def profile(series, extremes=True, stale=False):
    return SimpleNamespace(series=series, extremes=extremes, stale=stale, window_ms=30 * 86_400_000)


def pseries(labels, seas=None):
    return SimpleNamespace(
        labels=labels,
        seasonal=seas,
        range=RangeStats(n=10, p005=2.0, p995=8.0, envelope_lo=1.0, envelope_hi=9.0),
    )


def drawn(sid, labels, ts):
    return {"id": sid, "labels": labels, "ts": ts, "avg": [1.0] * len(ts)}


def test_band_follows_the_same_hour_of_the_week():
    t0 = 1_790_000_000_000 // HOUR * HOUR  # an hour boundary
    ts = [
        t0 + STEP,
        t0 + HOUR + STEP,
        t0 + 7 * 24 * HOUR + STEP,
    ]  # buckets end one step into an hour
    out = normal_payload(
        profile([pseries({"job": "a"}, seasonal())]), [drawn("s1", {"job": "a"}, ts)], STEP, "30d"
    )
    band = out["series"]["s1"]
    i0 = hour_of_week(t0)  # the bucket ending at t0+STEP covers [t0, t0+STEP): hour t0
    assert band["lo"] == [float(i0), float((i0 + 1) % 168), float(i0)]  # a week later: same bucket
    assert band["hi"] == [lo + 1 for lo in band["lo"]]
    assert "same hour of week" in out["label"] and "30d" in out["label"]


def test_band_week_boundary_wraps():
    # the last hour of Sunday is bucket 167; the next hour is bucket 0 (Monday 00:00 UTC)
    monday = next(
        t
        for t in range(1_790_000_000_000 // HOUR * HOUR, 1_790_000_000_000 + 8 * 24 * HOUR, HOUR)
        if hour_of_week(t) == 0
    )
    out = normal_payload(
        profile([pseries({}, seasonal())]),
        [drawn("s", {}, [monday + STEP, monday - HOUR + STEP])],
        STEP,
        "30d",
    )
    assert out["series"]["s"]["lo"] == [0.0, 167.0]


def test_hour_of_day_period_and_flat_fallback():
    t = 1_790_000_000_000 // HOUR * HOUR
    s = drawn("s", {}, [t + STEP, t + 24 * HOUR + STEP])
    day = normal_payload(profile([pseries({}, seasonal("hour_of_day", 24))]), [s], STEP, "30d")
    lo = day["series"]["s"]["lo"]
    assert lo[0] == lo[1]  # 24 hours apart: the same hour of day
    flat = normal_payload(profile([pseries({}, None)]), [s], STEP, "30d")
    assert flat["series"]["s"]["lo"] == [1.0, 1.0] and flat["series"]["s"]["hi"] == [
        9.0,
        9.0,
    ]  # envelope
    assert "flat" in flat["label"]
    robust = normal_payload(profile([pseries({}, None)], extremes=False), [s], STEP, "30d")
    assert robust["series"]["s"]["lo"] == [2.0, 2.0]  # robust quantiles without true extremes


def test_series_match_profile_series_by_labels_ignoring_name():
    ps = [pseries({"__name__": "x", "job": "a"}), pseries({"job": "b"})]
    out = normal_payload(
        profile(ps),
        [
            drawn("s1", {"job": "a"}, [STEP]),
            drawn("s2", {"job": "b"}, [STEP]),
            drawn("s3", {"job": "zzz"}, [STEP]),
        ],
        STEP,
        "30d",
    )
    assert set(out["series"]) == {"s1", "s2"} and out["unmatched"] == ["s3"]


def test_no_profile_or_no_match_says_why():
    assert normal_payload(None, [], STEP, "")["available"] is False
    out = normal_payload(
        profile([pseries({"job": "a"})]), [drawn("s", {"job": "b"}, [STEP])], STEP, "30d"
    )
    assert out["available"] is False and "matches" in out["reason"]


def test_limit_payload_reasons():
    assert "bounded_by" in limit_payload(None, None, None, {}, 100)["reason"]
    ctx = YContext(notes=["limit_unavailable: size could not be fetched (boom)"])
    assert limit_payload(None, ctx, None, {}, 100) == {
        "available": False,
        "reason": "size could not be fetched (boom)",
    }


# service -----------------------------------------------------------------------------------
NAMES = ["node_filesystem_avail_bytes", "node_filesystem_size_bytes", "app_odd"]


@pytest.fixture
async def svc(tmp_path):
    d = Discovery(tuple(MetricInfo(n) for n in NAMES), (), {}, None, 1.0, (), False)
    s = make_service(tmp_path, FakeSource(name="default", discovery=d, n_series=1))
    await s.learn("default")
    return s


async def shown(svc, expr="node_filesystem_avail_bytes"):
    ds = (await svc.query(expr, start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "q?").panel.id
    svc.profiles.cached = lambda source, e: None  # type: ignore[method-assign]
    await svc.y_context(pid)
    return pid


async def test_panel_data_reports_every_overlay_with_its_reason(svc):
    pid = await shown(svc)
    ov = svc.panel_data(pid, 800)["overlays"]
    assert ov["flags"] == {"normal": True, "limit": True, "ghost": False}
    assert ov["normal"]["available"] is False  # no profile yet
    assert (
        ov["limit"]["available"] is True and ov["limit"]["metric"] == "node_filesystem_size_bytes"
    )
    assert ov["limit"]["series"] and ov["limit"]["label"] == "limit node_filesystem_size_bytes"
    assert ov["ghost"] == {"available": True, "loaded": False, "label": "last week"}


async def test_unbounded_metric_has_no_limit_line(svc):
    pid = await shown(svc, "app_odd")
    assert svc.panel_data(pid, 800)["overlays"]["limit"]["available"] is False


async def test_normal_band_appears_once_the_profile_exists(svc):
    pid = await shown(svc)
    labels = json.loads(
        svc.datasets.get(svc.workspace.get_panel(pid).dataset_ids[0])[1].series.to_pylist()[0][
            "labels"
        ]
    )
    svc.profiles.cached = lambda source, e: profile([pseries(labels, seasonal())])  # type: ignore[method-assign]
    ov = svc.panel_data(pid, 800)["overlays"]
    assert ov["normal"]["available"] and len(ov["normal"]["series"]) == 1
    assert ov["normal"]["unmatched"] == []


async def test_switching_a_layer_off_keeps_its_availability_but_drops_its_data(svc):
    pid = await shown(svc)
    await svc.set_overlays(pid, "user", limit=False)
    ov = svc.panel_data(pid, 800)["overlays"]
    assert ov["flags"]["limit"] is False
    assert ov["limit"]["available"] is True and "series" not in ov["limit"]


async def test_ghost_fetches_last_week_once_and_draws_it_on_the_panel_grid(svc):
    pid = await shown(svc)
    out = await svc.set_overlays(pid, "user", ghost=True)
    assert out["overlays"]["ghost"] is True
    spec = ChartSpec.model_validate(svc.workspace.get_panel(pid).spec)
    ref = spec.references["week"]
    n_datasets = len(svc.datasets.list_metas())
    await svc.set_overlays(pid, "user", ghost=True)  # again: no second fetch
    assert len(svc.datasets.list_metas()) == n_datasets
    meta = svc.datasets.meta(spec.layers[0].data)
    g = svc.panel_data(pid, 800)["overlays"]["ghost"]
    assert g["loaded"] is True and g["series"]
    assert (
        g["series"][0]["ts"][0] == meta.start_ms + 0 or g["series"][0]["ts"][0] >= meta.start_ms
    )  # shifted onto the panel grid
    assert ref.shift_ms == 7 * 86_400_000
    await svc.set_overlays(pid, "user", ghost=False)
    off = svc.panel_data(pid, 800)["overlays"]["ghost"]
    assert off["loaded"] is True and "series" not in off  # fetched stays fetched, not drawn


async def test_only_given_flags_change_and_events_are_logged(svc):
    pid = await shown(svc)
    await svc.set_overlays(pid, "claude", normal=False)
    spec = ChartSpec.model_validate(svc.workspace.get_panel(pid).spec)
    assert spec.overlays.model_dump() == {"normal": False, "limit": True, "ghost": False}
    ev = [e for e in svc.ws.log.since(0) if e.type == "panel.overlays_set"][-1]
    assert (ev.actor, ev.klass, ev.payload["normal"]) == ("claude", "internal", False)


async def test_non_time_panels_have_no_overlays(svc):
    pid = await shown(svc)
    p = svc.workspace.get_panel(pid)
    spec = ChartSpec.model_validate(p.spec)
    spec.layers[0].mark = "heatmap"
    svc.workspace.set_spec(pid, spec.model_dump())
    with pytest.raises(ValueError, match="time-series"):
        await svc.set_overlays(pid, "user", normal=False)


async def test_mcp_set_overlays(svc):
    pid = await shown(svc)
    async with Client(build_mcp(svc, "http://x")) as c:
        res = await c.call_tool("set_overlays", {"panel": pid, "ghost": True, "normal": False})
        assert not res.is_error
        assert json.loads(res.content[0].text)["overlays"] == {
            "normal": False,
            "limit": True,
            "ghost": True,
        }
        bad = await c.call_tool("set_overlays", {"panel": "p99", "ghost": True})
        assert bad.is_error


def test_api_route_validates_and_sets(tmp_path):
    import asyncio

    s = make_service(tmp_path)
    ds = asyncio.run(s.query("up", start="now-2h", end="now-1h"))["dataset"]
    pid = s.show(ds, "q?").panel.id
    with TestClient(create_app(s, allowed_hosts=["testserver"])) as c:
        ok = c.post(f"/api/panels/{pid}/overlays", json={"limit": False})
        assert ok.status_code == 200 and ok.json()["overlays"]["limit"] is False
        assert c.post(f"/api/panels/{pid}/overlays", json={}).status_code == 400
        assert c.post(f"/api/panels/{pid}/overlays", json={"ghost": "yes"}).status_code == 400
        assert c.post("/api/panels/p99/overlays", json={"ghost": False}).status_code == 404
