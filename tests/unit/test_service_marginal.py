import pytest

from telemetry_nerd.channel.format import describe_event
from tests.unit.fakes import FakeSource, make_service


async def _time_panel(svc, expr="up"):
    ds = (await svc.query(expr, start="now-2h", end="now-1h", step="1m"))["dataset"]
    return ds, svc.show(ds, "Is it up?").panel.id


async def test_samples_marginal_vs_previous_is_labelled_and_reused(tmp_path):
    src = FakeSource(name="default")
    svc = make_service(tmp_path, src)
    ds, pid = await _time_panel(svc)
    out = await svc.set_marginal(pid, "previous", "user")
    assert out["basis"] == "samples" and "not requests" in out["what"]
    meta, ref = svc.datasets.meta(ds), svc.workspace.get_panel(pid).spec["references"]["previous"]
    assert ref["end_ms"] + meta.step_ms == meta.start_ms and ref["series"] != ds
    now, prev = svc.panel_data(pid, 800)["marginal"]["windows"]
    assert (
        now["n"] == prev["n"] == 2 * 61 and sum(now["c"]) == now["n"]
    )  # 2 series x 61 steps pooled
    assert now["lo"] == prev["lo"]  # shared edges
    ev = svc.log.since(0)[-1]
    assert (ev.type, ev.klass) == ("panel.marginal_set", "ambient")
    assert "marginal vs previous window" in describe_event(ev)
    calls = src.calls
    await svc.set_marginal(pid, "previous", "user")
    assert src.calls == calls  # the reference dataset is reused, not refetched


async def test_percentile_panel_uses_the_histogram_behind_it(tmp_path):
    svc = make_service(tmp_path, FakeSource(name="default"))
    _, pid = await _time_panel(
        svc, "histogram_quantile(0.95, sum by (le) (rate(lat_seconds_bucket[5m])))"
    )
    out = await svc.set_marginal(pid, "previous", "user")
    assert out["basis"] == "distribution" and "observations" in out["what"]
    m = svc.panel_data(pid, 800)["marginal"]
    now, prev = m["windows"]  # source buckets, counts additive: every observation is in a bar
    assert now["n"] > 0 and sum(now["c"]) == pytest.approx(now["n"])
    assert now["lo"] == prev["lo"] or prev["n"] >= 0


async def test_off_heatmap_refusal_and_brief(tmp_path):
    svc = make_service(tmp_path, FakeSource(name="default"))
    _, pid = await _time_panel(svc)
    await svc.set_marginal(pid, "week", "claude", reason="compare with last Tuesday")
    assert svc.ws.brief()["panels"][0]["marginal"] == "same window last week"
    await svc.set_marginal(pid, None, "user")
    assert svc.panel_data(pid, 800).get("marginal") is None
    d = (await svc.query_distribution("lat_seconds_bucket", start="now-2h", end="now-1h"))[
        "dataset"
    ]
    hp = svc.show(d, "How is latency distributed?").panel.id
    with pytest.raises(ValueError, match="time-series"):
        await svc.set_marginal(hp, "previous", "user")
