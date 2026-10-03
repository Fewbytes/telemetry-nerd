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
