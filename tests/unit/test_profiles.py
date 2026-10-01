"""T1 operating profile: target derivation, lazy compute, daily refresh, pairing (bead 2as.7)."""

from __future__ import annotations

import asyncio

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.analysis.profile import HOUR_MS
from telemetry_nerd.catalog.rules import facts_from_name
from telemetry_nerd.core.profiles import (
    ProfileRefused,
    ProfileUnavailable,
    profile_target,
)
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceUnavailable
from telemetry_nerd.sources.spec import SourceSpec

from .fakes import FakeSource, make_service

DAY = 24 * HOUR_MS
MONDAY = 1_788_739_200_000  # 2026-09-07 00:00 UTC


def truth(hour_start_ms):
    hod = (hour_start_ms // HOUR_MS) % 24
    dow = ((hour_start_ms - MONDAY) // DAY) % 7
    return 100 + 20 * np.sin(2 * np.pi * hod / 24) - 30 * (dow >= 5)


class Clock:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


class SeasonalSource(FakeSource):
    """Hourly seasonal data up to `clock`; records every request."""

    def __init__(self, clock, *, fail=False, **kw):
        super().__init__(n_series=1, **kw)
        self.clock = clock
        self.fail = fail
        self.requests: list[tuple[str, str, int, int, int]] = []
        self.noise = np.random.default_rng(0)

    def _result(self, rng: TimeRange, step_ms: int, spread: float) -> FetchResult:
        ts = np.arange(rng.start_ms, rng.end_ms + 1, step_ms)
        ts = ts[ts <= self.clock()]
        v = truth(ts - step_ms) + self.noise.normal(0, 2, ts.size)
        sid = series_id(self.name, {"instance": "a"})
        buckets = pa.table(
            {
                "ts_ms": ts.tolist(),
                "series_id": [sid] * ts.size,
                "avg": v.tolist(),
                "min": (v - spread).tolist(),
                "max": (v + spread).tolist(),
                "count": [240] * ts.size,
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {"series_id": [sid], "labels": [labels_json({"instance": "a"})]}, schema=SERIES_SCHEMA
        )
        return FetchResult(buckets, series)

    async def fetch(self, expr, rng, step_ms):
        self.requests.append(("fetch", expr, rng.start_ms, rng.end_ms, step_ms))
        await asyncio.sleep(0)
        if self.fail:
            raise SourceUnavailable("source returned HTTP 503", hint="retry shortly")
        return self._result(rng, step_ms, 5.0)

    async def fetch_values(self, expr, rng, step_ms):
        self.requests.append(("values", expr, rng.start_ms, rng.end_ms, step_ms))
        await asyncio.sleep(0)
        if self.fail:
            raise SourceUnavailable("source returned HTTP 503", hint="retry shortly")
        return self._result(rng, step_ms, 0.0)


DISCOVERY = Discovery(
    metrics=(
        MetricInfo("queue_depth", "gauge", "items waiting", None),
        MetricInfo("reqs_total", "counter", "requests", None),
        MetricInfo("lat_seconds_bucket"),
        MetricInfo("lat_seconds_sum"),
        MetricInfo("lat_seconds_count"),
    ),
    label_names=("job",),
    histograms={"lat_seconds": "classic"},
    cardinality=None,
    metadata_coverage=1.0,
    caveats=(),
    partial=False,
)
NOW = MONDAY + 60 * DAY + 30 * 60_000  # half past midnight, ~8.5 weeks after MONDAY


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def src(clock):
    s = SeasonalSource(clock, name="default", discovery=DISCOVERY)
    return s


@pytest.fixture
async def svc(tmp_path, src, clock):
    s = make_service(tmp_path, src, clock=clock)
    await s.learn("default")
    return s


# --- what is profiled ---------------------------------------------------------------------
def target(expr, res=15_000):
    return profile_target(expr, facts_from_name, res)


def test_gauge_selector_is_profiled_as_is():
    t = target('queue_depth{job="a"}')
    assert (t.expr, t.kind, t.rate_window_ms) == ('queue_depth{job="a"}', "level", None)


def test_counter_selector_is_profiled_as_its_rate_never_the_total():
    t = target('reqs_total{job="a"}')
    assert t.expr == 'rate(reqs_total{job="a"}[3615s])' and t.kind == "rate"
    assert t.rate_window_ms == HOUR_MS + 15_000
    # on a 1h-downsampled profile source a rate needs >= 2 samples: 4 x resolution
    assert target("reqs_total", res=HOUR_MS).expr == "rate(reqs_total[4h])"


def test_rate_windows_widen_to_the_profile_step_so_panels_share_a_profile():
    a = target("sum by (job) (rate(reqs_total[1m]))")
    b = target("sum by (job) (rate(reqs_total[$__rate_interval]))")
    assert a.expr == b.expr == "sum by (job) (rate(reqs_total[3615s]))"
    assert a.kind == "rate"
    assert target("rate(reqs_total[1d])").expr == "rate(reqs_total[1d])"  # never narrowed


def test_quantiles_stay_per_step_quantiles():
    t = target("histogram_quantile(0.99, sum by (le) (rate(lat_seconds_bucket[5m])))")
    assert t.kind == "quantile"
    assert t.expr == "histogram_quantile(0.99, sum by (le) (rate(lat_seconds_bucket[3615s])))"


def test_extensive_windows_are_not_widened():
    # increase over 5m is a different quantity from increase over an hour
    t = target("increase(reqs_total[5m])")
    assert t.expr == "increase(reqs_total[5m])" and t.kind == "level"


@pytest.mark.parametrize(
    "expr", ["lat_seconds_bucket", 'lat_seconds_bucket{le="0.1"}', "sum(reqs_total)"]
)
def test_histograms_and_raw_counter_totals_are_refused_with_a_hint(expr):
    with pytest.raises(ProfileRefused) as e:
        target(expr)
    assert e.value.hint


# --- lazy compute and daily refresh -------------------------------------------------------
async def test_nothing_is_computed_until_first_view(svc, src):
    assert svc.profiles.cached("default", "queue_depth") is None
    assert src.requests == []


async def test_profile_recovers_the_synthetic_season_through_the_service(svc, src):
    p = await svc.profiles.ensure("default", "queue_depth")
    assert p.kind == "level" and p.extremes and p.profiled_from == "default"
    assert p.step_ms == HOUR_MS and p.window_ms == 30 * DAY
    assert p.end_ms == NOW // HOUR_MS * HOUR_MS  # last complete hour
    assert p.end_ms - p.start_ms == 30 * DAY - HOUR_MS
    (s,) = p.series
    assert s.n == 720 and s.coverage == 1.0
    assert s.seasonal.period == "hour_of_week"
    levels = np.array([b.level for b in s.seasonal.buckets])
    assert np.mean(np.abs(levels - truth(MONDAY + np.arange(168) * HOUR_MS))) < 2
    assert p.pooled.envelope_hi > p.pooled.p995  # intra-hour maxima widen the envelope
    assert {r[0] for r in src.requests} == {"fetch"}
    assert all(r[4] == HOUR_MS for r in src.requests)


async def test_second_view_is_served_from_the_store(svc, src):
    first = await svc.profiles.ensure("default", "queue_depth")
    n = len(src.requests)
    again = await svc.profiles.ensure("default", "queue_depth")
    assert len(src.requests) == n and again.computed_at_ms == first.computed_at_ms
    cached = svc.profiles.cached("default", "queue_depth")
    assert cached is not None and not cached.stale and cached.id == first.id


async def test_profiles_refresh_daily(svc, src, clock):
    first = await svc.profiles.ensure("default", "queue_depth")
    clock.t += 23 * HOUR_MS
    assert (
        await svc.profiles.ensure("default", "queue_depth")
    ).computed_at_ms == first.computed_at_ms
    clock.t += 2 * HOUR_MS
    assert svc.profiles.cached("default", "queue_depth").stale
    n = len(src.requests)
    fresh = await svc.profiles.ensure("default", "queue_depth")
    assert fresh.computed_at_ms == clock.t and not fresh.stale
    assert fresh.end_ms == first.end_ms + 25 * HOUR_MS
    # the series cache keeps settled chunks: a daily refresh re-fetches only the newest one
    assert len(src.requests) - n == 1


async def test_concurrent_views_share_one_computation(svc, src):
    a, b = await asyncio.gather(
        svc.profiles.ensure("default", "queue_depth"), svc.profiles.ensure("default", "queue_depth")
    )
    assert a.id == b.id
    fetches = [r for r in src.requests if r[0] == "fetch"]
    assert len(fetches) == len({(r[2], r[3]) for r in fetches})  # no chunk fetched twice
    assert [ev.type for ev in svc.log.since(0)].count("profile.computed") == 1


async def test_failures_are_remembered_and_retried_after_an_hour(svc, src, clock):
    src.fail = True
    with pytest.raises(ProfileUnavailable) as e:
        await svc.profiles.ensure("default", "queue_depth")
    assert "503" in str(e.value)
    n = len(src.requests)
    with pytest.raises(ProfileUnavailable, match="failed recently"):
        await svc.profiles.ensure("default", "queue_depth")
    assert len(src.requests) == n  # not re-queried
    src.fail = False
    clock.t += HOUR_MS
    assert (await svc.profiles.ensure("default", "queue_depth")).series_total == 1
    types = [ev.type for ev in svc.log.since(0)]
    assert "profile.failed" in types and "profile.computed" in types


async def test_a_failed_refresh_keeps_serving_the_stale_profile(svc, src, clock):
    first = await svc.profiles.ensure("default", "queue_depth")
    clock.t += 25 * HOUR_MS
    src.fail = True
    with pytest.raises(ProfileUnavailable):
        await svc.profiles.ensure("default", "queue_depth")
    stale = await svc.profiles.ensure("default", "queue_depth")  # within the retry hour
    assert stale.stale and stale.computed_at_ms == first.computed_at_ms


async def test_counters_are_profiled_as_rates(svc, src):
    p = await svc.profiles.ensure("default", "reqs_total")
    assert p.kind == "rate" and p.expr == "rate(reqs_total[3615s])"
    assert p.rate_window_ms == HOUR_MS + 15_000
    assert {r[1] for r in src.requests} == {"rate(reqs_total[3615s])"}
    assert not p.extremes and "no_intra_hour_extremes" in p.caveats


async def test_refusals_never_reach_the_source(svc, src):
    with pytest.raises(ProfileRefused):
        await svc.profiles.ensure("default", "lat_seconds_bucket")
    assert svc.profiles.cached("default", "lat_seconds_bucket") is None
    assert src.requests == []


# --- catalog link and events --------------------------------------------------------------
async def test_a_catalogued_metric_points_at_its_profile(svc):
    p = await svc.profiles.ensure("default", "reqs_total")
    ref = svc.ws.catalog_entry("default", "reqs_total").fields["operating_profile_ref"]
    assert ref.value == p.id and ref.origin == "stats"
    assert svc.profiles.by_id(p.id).expr == p.expr
    # a recompute does not rewrite an unchanged reference
    ts = ref.ts_ms
    await svc.profiles.ensure("default", "reqs_total", force=True)
    assert svc.ws.catalog_entry("default", "reqs_total").fields["operating_profile_ref"].ts_ms == ts


async def test_compute_logs_one_internal_event(svc):
    p = await svc.profiles.ensure("default", 'queue_depth{job="a"}')
    evs = [ev for ev in svc.log.since(0) if ev.type == "profile.computed"]
    assert len(evs) == 1 and evs[0].object_id == p.id and evs[0].klass == "internal"
    # a filtered selector is not the metric: no catalog link
    assert "operating_profile_ref" not in svc.ws.catalog_entry("default", "queue_depth").fields


# --- profile source pairing ---------------------------------------------------------------
async def test_a_paired_downsampled_source_serves_the_profile(tmp_path, clock):
    main = SeasonalSource(clock, name="default", discovery=DISCOVERY)
    svc = make_service(tmp_path, main, clock=clock)
    await svc.learn("default")
    down = SeasonalSource(clock, name="wm-1h", resolution_ms=HOUR_MS)
    svc.sources.add(SourceSpec(name="wm-1h", url="http://down", resolution_ms=HOUR_MS), down)
    svc.sources.attach(
        "default",
        main,
        SourceSpec(name="default", url="http://main", profile_source="wm-1h"),
    )
    p = await svc.profiles.ensure("default", "reqs_total")
    assert p.source == "default" and p.profiled_from == "wm-1h"
    assert p.expr == "rate(reqs_total[4h])" and p.rate_window_ms == 4 * HOUR_MS
    assert main.requests == [] and down.requests


async def test_an_unavailable_pairing_falls_back_and_says_so(tmp_path, clock):
    main = SeasonalSource(clock, name="default")
    svc = make_service(tmp_path, main, clock=clock)
    svc.sources.attach(
        "default", main, SourceSpec(name="default", url="http://main", profile_source="gone")
    )
    p = await svc.profiles.ensure("default", "queue_depth")
    assert p.profiled_from == "default" and "profile_source_unavailable" in p.caveats


def test_profile_source_must_be_another_source():
    with pytest.raises(ValueError, match="another source"):
        SourceSpec(name="a", url="http://x", profile_source="a")


# --- on view ------------------------------------------------------------------------------
async def _drain(svc):
    for _ in range(100):
        if not svc.profiles._inflight:
            return
        await asyncio.sleep(0)
    raise AssertionError("background profile did not finish")


async def test_request_schedules_a_background_compute(svc):
    assert svc.profiles.request("default", "queue_depth") is None
    await asyncio.sleep(0)
    await _drain(svc)
    assert svc.profiles.request("default", "queue_depth") is not None


async def test_request_without_an_event_loop_only_reads(svc):
    def sync():
        return svc.profiles.request("default", "queue_depth")

    assert await asyncio.to_thread(sync) is None
    assert svc.profiles._inflight == {}


async def test_showing_a_time_series_panel_profiles_it_lazily(svc):
    svc.auto_profile = True
    out = await svc.query("queue_depth", start="now-1h", step="1m")
    svc.show(out["dataset"], "How deep is the queue?")
    await asyncio.sleep(0)
    await _drain(svc)
    assert svc.profiles.cached("default", "queue_depth") is not None


async def test_operating_profile_summary_is_compact(svc):
    out = await svc.operating_profile("queue_depth")
    assert out["kind"] == "level" and out["window"] == "30d" and out["step"] == "1h"
    (row,) = out["series"]
    assert row["seasonal"]["period"] == "hour_of_week" and row["seasonal"]["tz"] == "UTC"
    assert "buckets" not in row["seasonal"]
    assert row["n"] == 720 and row["coverage"] == 1.0


# --- MCP ----------------------------------------------------------------------------------
async def test_mcp_operating_profile_and_profile_source_pairing(svc):
    import json

    from telemetry_nerd.mcp.server import build_mcp
    from tests.unit.test_mcp_sources import call, text

    mcp = build_mcp(svc, "http://x")
    r = await call(mcp, "operating_profile", {"expr": "reqs_total"})
    assert not r.is_error, text(r)
    out = json.loads(text(r))
    assert out["kind"] == "rate" and out["expr"] == "rate(reqs_total[3615s])"
    r = await call(mcp, "operating_profile", {"expr": "lat_seconds_bucket"})
    assert r.is_error and "hint" in text(r)
    r = await call(
        mcp,
        "source_connect",
        {"name": "wm", "url": "http://wm.test", "profile_source": "wm-1h"},
    )
    assert not r.is_error, text(r)
    assert json.loads(text(r))["source"]["profile_source"] == "wm-1h"
