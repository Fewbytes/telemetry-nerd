"""A source's resolution is learned from its series' scrape spacing (bead wbw).

Round 3 read a 5 s queue-sim source at the configured default 15 s: check_littles_law refused
1 m windows and analyze had too few points for a 3x arrival surge.
"""

import httpx
import respx

from telemetry_nerd.sources.promql import DEFAULT_RESOLUTION_MS, PromQLSource
from telemetry_nerd.sources.spec import SourceSpec
from tests.unit.fakes import make_service

BASE = "http://vm.test"
T0 = 1_791_000_000.0


def _series(job: str | None, every_s: float, n: int = 120, **labels) -> dict:
    metric = {"__name__": "up", **labels} | ({"job": job} if job else {})
    return {"metric": metric, "values": [[T0 + i * every_s, "1"] for i in range(n)]}


def _vm(by_query: dict[str, list[dict]]):
    """A VM answering instant range-vector queries `<sel>[10m]` from by_query[sel]."""

    def answer(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        sel = q.removesuffix("[10m]")
        result = by_query.get(sel, [])
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    return answer


@respx.mock
async def test_the_scrape_spacing_of_up_becomes_the_resolution():
    respx.get(f"{BASE}/api/v1/query").mock(
        side_effect=_vm({"up": [_series("qs", 5, instance=f"i{k}") for k in range(3)]})
    )
    src = PromQLSource("vm", BASE)
    assert (src.resolution_ms, src.resolution_origin) == (DEFAULT_RESOLUTION_MS, "assumed")
    info = await src.learn_resolution()
    assert src.resolution_ms == 5_000 and src.resolution_origin == "learned"
    assert info["resolution"] == "5s" and info["origin"] == "learned"
    assert info["measured"]["by_job"] == {"qs": "5s"} and info["measured"]["probed"] == "up"
    assert "|5000" in src.identity  # cached queries at the old resolution are not reused


@respx.mock
async def test_jobs_at_different_intervals_take_the_coarsest_and_say_so():
    respx.get(f"{BASE}/api/v1/query").mock(
        side_effect=_vm({"up": [_series("fast", 5), _series("fast", 5), _series("node", 30)]})
    )
    src = PromQLSource("vm", BASE)
    info = await src.learn_resolution()
    assert src.resolution_ms == 30_000
    assert info["measured"]["by_job"] == {"fast": "5s", "node": "30s"}
    assert "coarsest" in info["measured"]["note"]


@respx.mock
async def test_a_configured_resolution_overrides_and_a_mismatch_is_stated():
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=_vm({"up": [_series("qs", 5)]}))
    src = PromQLSource("vm", BASE, resolution_ms=15_000)
    info = await src.learn_resolution()
    assert src.resolution_ms == 15_000 and info["origin"] == "configured"
    assert info["measured"]["resolution"] == "5s"
    assert "scraped every 5s" in info["mismatch"]


@respx.mock
async def test_push_sources_without_up_are_measured_on_a_candidate_metric():
    respx.get(f"{BASE}/api/v1/query").mock(
        side_effect=_vm({"traces_span_metrics_calls_total": [_series(None, 10)]})
    )
    src = PromQLSource("vm", BASE)
    info = await src.learn_resolution(["process_cpu", "traces_span_metrics_calls_total"])
    assert src.resolution_ms == 10_000
    assert info["measured"]["by_job"] == {"(no job label)": "10s"}
    assert info["measured"]["probed"] == "traces_span_metrics_calls_total"


@respx.mock
async def test_no_recent_samples_keeps_the_assumed_resolution_and_says_why():
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=_vm({"up": [_series("qs", 5, n=2)]}))
    src = PromQLSource("vm", BASE)
    info = await src.learn_resolution()
    assert src.resolution_ms == DEFAULT_RESOLUTION_MS and info["origin"] == "assumed"
    assert "unknown" in info["measured"]["note"] and "source_learn" in info["note"]


@respx.mock
async def test_jitter_does_not_move_the_median_spacing():
    jittered = {
        "metric": {"job": "qs"},
        "values": [[T0 + i * 5 + (0.4 if i % 7 == 0 else 0.0), "1"] for i in range(60)],
    }
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=_vm({"up": [jittered]}))
    src = PromQLSource("vm", BASE)
    await src.learn_resolution()
    assert src.resolution_ms == 5_000


# --- service: connect, learn, status, lazy retry ----------------------------------------------
def _promql_service(tmp_path, clock):
    svc = make_service(tmp_path, clock=clock, factory=PromQLSource.from_spec)
    return svc


@respx.mock
async def test_connect_and_status_report_the_learned_resolution(tmp_path):
    respx.get(f"{BASE}/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json={"data": {"version": "v1"}})
    )
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=_vm({"up": [_series("qs", 5)]}))
    svc = _promql_service(tmp_path, lambda: 1_791_000_000_000)
    out = await svc.source_connect(SourceSpec(name="qs", url=BASE, flavor="victoriametrics"))
    assert out["resolution"]["resolution"] == "5s" and out["resolution"]["origin"] == "learned"
    assert svc.sources["qs"].resolution_ms == 5_000
    status = await svc.source_status("qs")
    assert status["reachable"] and status["resolution"]["measured"]["by_job"] == {"qs": "5s"}
    [entry] = [e for e in svc.source_list() if e["name"] == "qs"]
    assert entry["resolution"]["origin"] == "learned" and entry["resolution_ms"] is None


@respx.mock
async def test_an_unlearned_source_retries_before_queries_at_most_once_a_minute(tmp_path):
    respx.get(f"{BASE}/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    data: dict[str, list[dict]] = {}  # a fresh VM: nothing scraped yet
    probe = respx.get(f"{BASE}/api/v1/query").mock(side_effect=_vm(data))
    now = [1_791_000_000_000]
    svc = _promql_service(tmp_path, lambda: now[0])
    await svc.source_connect(SourceSpec(name="qs", url=BASE, flavor="victoriametrics"))
    assert svc.sources["qs"].resolution_origin == "assumed"
    calls = probe.call_count
    data["up"] = [_series("qs", 5)]
    await svc.ensure_resolution("qs")  # tried just now: no new probe
    assert probe.call_count == calls and svc.sources["qs"].resolution_origin == "assumed"
    now[0] += 61_000
    await svc.ensure_resolution("qs")
    assert svc.sources["qs"].resolution_ms == 5_000
    calls = probe.call_count
    now[0] += 3_600_000
    await svc.ensure_resolution("qs")  # learned: never re-probed implicitly
    assert probe.call_count == calls


# --- what a learned 5 s resolution changes downstream ------------------------------------------
def test_auto_step_follows_a_resolution_below_15s():
    from telemetry_nerd.core.service import auto_step
    from telemetry_nerd.model.time import TimeRange

    ten_min = TimeRange(0, 600_000)
    assert auto_step(ten_min, 5_000) == 5_000  # was 15 s: the nice steps started there
    assert auto_step(ten_min, 15_000) == 15_000
    assert auto_step(TimeRange(0, 3_600_000), 5_000) == 10_000  # ~600 buckets over 1h
    assert auto_step(TimeRange(0, 6 * 3_600_000), 5_000) == 60_000


def test_an_insufficient_series_smoothed_by_its_own_window_gets_a_hint():
    from types import SimpleNamespace

    from telemetry_nerd.core.series_diagnostics import window_hint

    meta = SimpleNamespace(
        expr="sum(rate(http_requests_total[1m]))", step_ms=5_000, resolution_ms=5_000,
        start_ms=0, end_ms=720_000,
    )  # fmt: skip
    hint = window_hint(meta)
    assert hint and "[1m] window spans 12 steps" in hint and "20s" in hint and "~12" in hint
    assert window_hint(SimpleNamespace(**{**vars(meta), "expr": "sum(rate(x[20s]))"})) is None
    assert window_hint(SimpleNamespace(**{**vars(meta), "expr": "up"})) is None
    # at 15 s the 1m window is the rate interval already: nothing shorter to suggest
    assert window_hint(SimpleNamespace(**{**vars(meta), "step_ms": 15_000,
                                          "resolution_ms": 15_000})) is None  # fmt: skip


@respx.mock
async def test_the_measured_spacing_is_rounded_to_whole_seconds():
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=_vm({"up": [_series("qs", 5.038)]}))
    src = PromQLSource("vm", BASE)
    await src.learn_resolution()
    assert src.resolution_ms == 5_000
