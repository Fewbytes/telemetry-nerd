import pytest

from telemetry_nerd.core.service import ChartRejected, auto_step
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import LimitExceeded, SourceError
from tests.unit.fakes import FakeSource, NonFiniteSource, PartialSource, make_service


def test_auto_step_targets_about_600_buckets():
    assert auto_step(TimeRange(0, 3_600_000), 15_000) == 15_000
    assert auto_step(TimeRange(0, 6 * 3_600_000), 15_000) == 60_000
    assert auto_step(TimeRange(0, 7 * 86_400_000), 15_000) == 1_800_000


def test_auto_step_never_below_resolution():
    assert auto_step(TimeRange(0, 60_000), 30_000) == 30_000


async def test_query_returns_handle_and_compact_summary(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("up", start="now-2h", end="now-1h", step="1m")
    assert out["dataset"] == "d1"
    s = out["summary"]
    assert s["series_count"] == 2
    assert s["step"] == "1m"
    assert s["series"][0]["max"] == 2.5  # sorted by max, descending
    assert s["caveats"] == []
    assert len(str(out)) < 2048


async def test_query_flags_settling_and_fake_resolution(tmp_path):
    svc = make_service(tmp_path, FakeSource(resolution_ms=60_000))
    out = await svc.query("up", start="now-10m", end="now", step="15s")
    assert set(out["summary"]["caveats"]) >= {"settling", "fake_resolution"}


async def test_unknown_source_has_hint(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError) as exc:
        await svc.query("up", source="nope")
    assert "default" in (exc.value.hint or "")


async def test_show_infers_unit_from_metric_name_and_persists_it(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("tn_demo_latency_seconds", start="now-2h", end="now-1h"))["dataset"]
    res = svc.show(ds, "How slow is it?")
    assert res.panel.spec["y"]["unit"] == "s"
    assert res.panel.spec["y"]["unit_provenance"] == "inferred from metric name"
    assert res.issues == []  # no "units" warning when the unit is known
    # the unit is persisted: reloaded from the workspace DB, not just in memory
    assert svc.workspace.get_panel(res.panel.id).spec["y"]["unit"] == "s"


async def test_show_without_inferable_unit_still_warns(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    res = svc.show(ds, "Up?")
    assert res.panel.spec["y"]["unit"] is None
    assert [i.rule for i in res.issues] == ["units"]


async def test_show_agent_learned_unit_overrides_inference_and_persists(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("tn_demo_latency_seconds", start="now-2h", end="now-1h"))["dataset"]
    res = svc.show(ds, "How slow is it?", unit="ms")  # Claude knows it's actually ms
    assert res.panel.spec["y"]["unit"] == "ms"
    assert res.panel.spec["y"]["unit_provenance"] == "provided by claude"
    assert svc.workspace.get_panel(res.panel.id).spec["y"] == {
        "range_mode": "data",
        "unit": "ms",
        "unit_provenance": "provided by claude",
        "label": None,
    }


async def test_show_creates_panel_and_publishes_event(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    q = svc.log.subscribe()  # after query, so dataset.created is not in the queue
    res = svc.show(ds, "Are both instances stable?")
    assert res.panel.id == "p1"
    assert res.panel.dataset_ids == [ds]
    assert [i.rule for i in res.issues] == ["units"]
    ev = q.get_nowait()
    assert (ev["type"], ev["object_id"], ev["actor"]) == ("panel.created", "p1", "claude")


async def test_show_rejects_spaghetti(tmp_path):
    svc = make_service(tmp_path, FakeSource(n_series=6))
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    with pytest.raises(ChartRejected) as exc:
        svc.show(ds, "Which instance is slow?")
    assert exc.value.issues[0].rule == "series_budget"


async def test_show_requires_question(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    with pytest.raises(ValueError):
        svc.show(ds, " ")


async def test_panel_data_respects_width(tmp_path):
    svc = make_service(tmp_path)
    ds = (await svc.query("up", start="now-6h", end="now-1h", step="15s"))["dataset"]
    panel = svc.show(ds, "Stable?").panel
    data = svc.panel_data(panel.id, width_px=100)
    assert len(data["series"]) == 2
    assert all(len(s["ts"]) <= 101 for s in data["series"])
    assert data["effective_step_ms"] > 15_000
    assert sorted(s["labels"]["instance"] for s in data["series"]) == ["i0", "i1"]
    assert data["dataset"]["expr"] == "up"


async def test_cache_is_keyed_on_source_identity_not_name(tmp_path):
    a = FakeSource(name="same", identity="id-a")
    svc = make_service(tmp_path, a)
    await svc.query("up", start="now-2h", end="now-1h", step="1m")
    calls_a = a.calls
    svc.sources.attach("default", FakeSource(name="same", identity="id-b"))
    await svc.query("up", start="now-2h", end="now-1h", step="1m")
    assert svc.sources["default"].calls > 0
    assert a.calls == calls_a


async def test_partial_fetch_is_flagged_in_summary_and_panel(tmp_path):
    svc = make_service(tmp_path, PartialSource())
    out = await svc.query("up", start="now-2h", end="now-1h", step="1m")
    assert "partial" in out["summary"]["caveats"]
    panel = svc.show(out["dataset"], "q?").panel
    assert "partial" in svc.panel_data(panel.id, 800)["caveats"]


async def test_non_finite_values_flagged(tmp_path):
    svc = make_service(tmp_path, NonFiniteSource())
    out = await svc.query("up", start="now-2h", end="now-1h", step="1m")
    assert "non_finite" in out["summary"]["caveats"]


@pytest.mark.parametrize("step", ["0s", "0ms"])
async def test_zero_step_is_typed_error_with_hint(tmp_path, step):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError) as exc:
        await svc.query("up", start="now-1h", end="now", step=step)
    assert exc.value.hint


async def test_too_many_buckets_is_limit_exceeded_before_any_fetch(tmp_path):
    src = FakeSource()
    svc = make_service(tmp_path, src)
    with pytest.raises(LimitExceeded) as exc:
        await svc.query("up", start="now-30d", end="now", step="1s")
    assert "coarser step" in (exc.value.hint or "")
    assert src.calls == 0
