import pytest

from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.core.events import Event, classify
from tests.unit.fakes import make_service


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


async def test_rescope_refuses_a_code_output_panel(tmp_path):
    from telemetry_nerd.datasets.store import Lineage
    from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
    from telemetry_nerd.model.time import TimeRange

    svc = make_service(tmp_path)
    # a code output has no source expr to re-query (same refusal reframe() uses)
    code_ds = svc.datasets.put(
        source="default", expr="code", rng=TimeRange(0, 1000), step_ms=1000, resolution_ms=1000,
        result=FetchResult(BUCKET_SCHEMA.empty_table(), SERIES_SCHEMA.empty_table()),
        lineage=Lineage(producer={"kind": "code", "node": "n1", "output": "o1"}),
    )
    code_panel = svc.show(code_ds.id, "computed?")
    with pytest.raises(ValueError, match="fixed data"):
        await svc.rescope(code_panel.panel.id, "now-3h", "now", "user")
