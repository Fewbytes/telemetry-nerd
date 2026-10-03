import pytest

from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.core.events import Event, classify
from tests.unit.fakes import FakeSource, make_service


async def test_query_falls_back_to_now_1h_when_no_default_set(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("rate(node_cpu_seconds_total[5m])")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3600_000, rel=0.05)


async def test_query_uses_the_workspace_default_range_when_set(tmp_path):
    svc = make_service(tmp_path)
    svc.set_default_range("now-3h")
    out = await svc.query("rate(node_cpu_seconds_total[5m])")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3 * 3600_000, rel=0.05)


async def test_explicit_start_overrides_the_workspace_default(tmp_path):
    svc = make_service(tmp_path)
    svc.set_default_range("now-3h")
    out = await svc.query("rate(node_cpu_seconds_total[5m])", start="now-1h")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3600_000, rel=0.05)


def test_set_default_range_rejects_an_unparseable_value(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ValueError):
        svc.set_default_range("not a time")


def test_get_default_range_is_now_1h_before_anything_is_set(tmp_path):
    svc = make_service(tmp_path)
    assert svc.get_default_range() == "now-1h"


def test_panel_rescoped_is_ambient_for_a_user_actor():
    assert classify("user", "panel.rescoped", {"from": "p1"}) == "ambient"


def test_panel_rescoped_is_internal_for_a_claude_actor():
    assert classify("claude", "panel.rescoped", {"from": "p1"}) == "internal"


def test_describe_event_renders_panel_rescoped():
    e = Event(1, 0, "user", "panel.rescoped", "p2", "ambient", {"from": "p1"})
    assert describe_event(e) == "user rescoped p1 to p2"


async def test_preview_returns_a_dataset_over_the_new_range_without_touching_the_panel(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    pid = shown.panel.id
    before = svc.workspace.get_panel(pid)
    out = await svc.preview(pid, "now-3h", "now")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3 * 3600_000, rel=0.05)
    after = svc.workspace.get_panel(pid)
    assert after == before  # untouched: same question, status, spec, dataset_ids


async def test_preview_logs_no_ambient_or_intentional_event(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    before_seq = svc.log.last_seq
    await svc.preview(shown.panel.id, "now-3h", "now")
    new_events = svc.log.since(before_seq)
    assert all(e.klass == "internal" for e in new_events)


async def test_rescope_makes_a_new_panel_and_leaves_the_old_one_untouched(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    pid = shown.panel.id
    before = svc.workspace.get_panel(pid)
    res = await svc.rescope(pid, "now-3h", "now", "user")
    assert res.panel.id != pid
    assert svc.workspace.get_panel(pid) == before
    meta = svc.datasets.meta(res.panel.dataset_ids[0])
    assert meta.end_ms - meta.start_ms == pytest.approx(3 * 3600_000, rel=0.05)
    assert res.panel.spec["auto"]["transform"] == "rescope"
    assert res.panel.spec["auto"]["source_dataset"] == before.dataset_ids[0]
    assert "rescoped from" in res.panel.spec["auto"]["reason"]


async def test_rescope_logs_a_panel_rescoped_event(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    pid = shown.panel.id
    before_seq = svc.log.last_seq
    res = await svc.rescope(pid, "now-3h", "now", "user")
    [e] = [e for e in svc.log.since(before_seq) if e.type == "panel.rescoped"]
    assert e.object_id == res.panel.id and e.payload["from"] == pid


async def test_rescope_a_fleet_panel_carries_over_its_options(tmp_path):
    svc = make_service(tmp_path, source=FakeSource(n_series=6))
    d = (await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"]
    svc.fleets.summary(d, by=["instance"], scale="log")  # seed a non-default config
    shown = svc.show(d, "per-core?", mark="fleet", bounds_lo=0, bounds_hi=100)
    pid = shown.panel.id
    res = await svc.rescope(pid, "now-3h", "now", "user")
    assert res.panel.id != pid
    assert res.panel.spec["layers"][0]["mark"] == "fleet"
    new_cfg = svc.fleets.last_config(res.panel.dataset_ids[0])
    assert new_cfg["by"] == ["instance"]
    assert new_cfg["scale"] == "log"


async def test_rescope_an_spc_panel_reuses_its_baseline_when_it_still_fits(tmp_path):
    from telemetry_nerd.charts.spec import Window

    svc = make_service(tmp_path)
    d = (await svc.query("rate(node_cpu_seconds_total[5m])", start="now-6h", end="now"))["dataset"]
    meta = svc.datasets.meta(d)
    baseline_start, baseline_end = meta.start_ms, meta.start_ms + meta.step_ms * 3
    shown = svc.show(
        d,
        "spc?",
        mark="spc",
        windows=[Window(start_ms=baseline_start, end_ms=baseline_end)],
    )
    pid = shown.panel.id
    res = await svc.rescope(pid, "now-6h", "now", "user")
    assert res.panel.spec["layers"][0]["mark"] == "spc"
    w = res.panel.spec["layers"][0]["windows"][0]
    assert w["start_ms"] == baseline_start and w["end_ms"] == baseline_end


async def test_rescope_an_spc_panel_raises_when_the_baseline_no_longer_fits(tmp_path):
    from telemetry_nerd.charts.spec import Window

    svc = make_service(tmp_path)
    d = (await svc.query("rate(node_cpu_seconds_total[5m])", start="now-6h", end="now"))["dataset"]
    meta = svc.datasets.meta(d)
    baseline_start, baseline_end = meta.start_ms, meta.start_ms + meta.step_ms * 3
    shown = svc.show(
        d,
        "spc?",
        mark="spc",
        windows=[Window(start_ms=baseline_start, end_ms=baseline_end)],
    )
    pid = shown.panel.id
    with pytest.raises(ValueError, match="baseline"):
        await svc.rescope(pid, "now-10m", "now", "user")  # new range excludes the old baseline


async def test_rescope_a_seasonal_panel_raises_unsupported(tmp_path):
    svc = make_service(tmp_path)
    d = (await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"]
    await svc.compare_seasonal(d, cycles=["1d"])
    pid = svc.show(d, "seasonal?", mark="seasonal").panel.id
    with pytest.raises(ValueError, match="rescope_unsupported_for_mark"):
        await svc.rescope(pid, "now-3h", "now", "user")


async def test_rescope_refuses_a_code_output_panel(tmp_path):
    from telemetry_nerd.datasets.store import Lineage
    from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
    from telemetry_nerd.model.time import TimeRange

    svc = make_service(tmp_path)
    # a code output has no source expr to re-query (same refusal reframe() uses)
    code_ds = svc.datasets.put(
        source="default",
        expr="code",
        rng=TimeRange(0, 1000),
        step_ms=1000,
        resolution_ms=1000,
        result=FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table()),
        lineage=Lineage(producer={"kind": "code", "node": "n1", "output": "o1"}),
    )
    code_panel = svc.show(code_ds.id, "computed?")
    with pytest.raises(ValueError, match="fixed data"):
        await svc.rescope(code_panel.panel.id, "now-3h", "now", "user")


def test_set_default_range_rejects_a_value_that_does_not_start_before_now(tmp_path):
    svc = make_service(tmp_path)
    for value in ("now", str(svc.clock() + 3_600_000)):
        with pytest.raises(ValueError, match="before now"):
            svc.set_default_range(value)
    assert svc.get_default_range() == "now-1h"  # nothing stored


async def _filtered_panel(svc):
    d = (await svc.query("queue_depth", start="now-2d", end="now", step="1m"))["dataset"]
    f = svc.filter(d, "lowpass", "1h", "trend?")["dataset"]
    return svc.show(f, "trend?").panel


async def test_rescope_refuses_a_filtered_panel(tmp_path):
    svc = make_service(tmp_path)
    p = await _filtered_panel(svc)
    assert p.spec["signal"] is not None  # line+envelope: passes the mark gate
    before = svc.workspace.list_panels()
    with pytest.raises(ValueError, match="rescope_unsupported_for_derived"):
        await svc.rescope(p.id, "now-3h", "now", "user")
    assert svc.workspace.list_panels() == before  # no unfiltered panel slipped in


async def test_preview_refuses_a_filtered_panel(tmp_path):
    svc = make_service(tmp_path)
    p = await _filtered_panel(svc)
    with pytest.raises(ValueError, match="rescope_unsupported_for_derived"):
        await svc.preview(p.id, "now-3h", "now")


async def test_rescope_picks_a_step_for_the_new_range_not_the_old_one(tmp_path):
    svc = make_service(tmp_path)
    d = await svc.query("rate(node_cpu_seconds_total[5m])", start="now-15m", end="now")
    old = svc.datasets.meta(d["dataset"])
    pid = svc.show(d["dataset"], "cpu?").panel.id
    rescoped = (await svc.rescope(pid, "now-7d", "now", "user")).panel.dataset_ids[0]
    previewed = (await svc.preview(pid, "now-7d", "now"))["dataset"]
    for ds in (rescoped, previewed):
        meta = svc.datasets.meta(ds)
        assert meta.step_ms > old.step_ms
        assert (meta.end_ms - meta.start_ms) // meta.step_ms < 5_000  # not ~40k buckets


async def test_rescope_a_fleet_panel_reads_the_panels_config_not_the_last_fleet_call(tmp_path):
    svc = make_service(tmp_path, source=FakeSource(n_series=6))
    d = (await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"]
    svc.fleets.summary(d, by=["instance"], scale="log")
    pid = svc.show(d, "per-core?", mark="fleet").panel.id
    svc.fleets.summary(d)  # a later fleet() on the same dataset, with default options
    res = await svc.rescope(pid, "now-3h", "now", "user")
    assert res.panel.spec["layers"][0]["fleet"]["by"] == ["instance"]
    assert res.panel.spec["layers"][0]["fleet"]["scale"] == "log"


async def test_rescope_refuses_an_spc_panel_whose_baseline_is_a_reference(tmp_path):
    svc = make_service(tmp_path)
    d = (await svc.query("queue_depth", start="now-6h", end="now", step="1m"))["dataset"]
    await svc.analyze_reference(d, "previous")
    p = svc.show(d, "shift vs previous?", mark="spc").panel
    assert p.spec["layers"][0]["spc"] is not None and p.spec["layers"][0]["windows"] == []
    before = svc.workspace.list_panels()
    with pytest.raises(ValueError, match="rescope_unsupported_for_spc_reference"):
        await svc.rescope(p.id, "now-3h", "now", "user")
    assert svc.workspace.list_panels() == before


async def test_rescope_records_y_context_like_every_new_time_panel(tmp_path):
    svc = make_service(tmp_path)
    pid = svc.show((await svc.query("node_memory_MemAvailable_bytes"))["dataset"], "mem?").panel.id
    res = await svc.rescope(pid, "now-3h", "now", "user")
    assert svc.workspace.get_panel(res.panel.id).spec["y"]["context"] is not None


async def test_rescope_carries_unit_bounds_and_quantiles(tmp_path):
    svc = make_service(tmp_path)
    d = (await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"]
    pid = svc.show(
        d, "cpu?", unit="ratio", mark="line+envelope", quantiles=[0.5, 0.99],
        bounds_lo=0, bounds_hi=100,
    ).panel.id  # fmt: skip
    res = await svc.rescope(pid, "now-3h", "now", "user")
    spec = res.panel.spec
    assert spec["y"]["unit"] == "ratio"
    assert spec["y"]["unit_provenance"].startswith("provided by")
    assert spec["y"]["asserted_bounds"] == {"lo": 0, "hi": 100, "by": "claude"}
    assert spec["layers"][0]["quantiles"] == [0.5, 0.99]


async def test_mcp_query_without_start_uses_the_workspace_default_range(tmp_path):
    import json

    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp

    svc = make_service(tmp_path)
    svc.set_default_range("now-3h")
    async with Client(build_mcp(svc, "http://x")) as c:
        res = await c.call_tool("query", {"expr": "rate(node_cpu_seconds_total[5m])"})
        assert not res.is_error
    meta = svc.datasets.meta(json.loads(res.content[0].text)["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3 * 3600_000, rel=0.05)
