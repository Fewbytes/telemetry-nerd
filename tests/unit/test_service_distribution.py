import json

import pytest

from telemetry_nerd.charts.spec import Window
from telemetry_nerd.core.service import ChartRejected
from telemetry_nerd.sources.base import SourceError
from tests.unit.fakes import FakeSource, make_service


async def test_query_distribution_builds_dataset_and_summary(tmp_path):
    src = FakeSource()
    svc = make_service(tmp_path, src)
    out = await svc.query_distribution(
        'lat_seconds_bucket{job="a"}', by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    meta, _ = svc.datasets.get_distribution(out["dataset"])
    assert meta.expr == 'sum by (le, vmrange, instance) (increase(lat_seconds_bucket{job="a"}[1m]))'
    assert meta.histogram == {"selector": 'lat_seconds_bucket{job="a"}', "by": ["instance"]}
    s = out["summary"]
    assert s["representation"] == "distribution" and s["n_min"] == 20
    assert s["buckets"] == "classic le buckets: 0.1, 1, 10"
    row = s["series"][0]  # sorted by n_total: instance i1 (x2)
    assert row["labels"] == {"instance": "i1"}
    assert (row["columns"], row["missing_columns"], row["low_n_columns"]) == (61, 0, 0)
    assert row["n_total"] == 200 * 61
    assert row["quantile_buckets"] == {
        "p50": ["-Inf", 0.1],
        "p90": ["-Inf", 0.1],
        "p99": [0.1, 1.0],
    }
    assert len(json.dumps(out)) < 2048
    assert src.hist_selectors == ['lat_seconds_bucket{job="a"}']


async def test_distribution_step_must_cover_two_scrapes(tmp_path):
    svc = make_service(tmp_path, FakeSource(resolution_ms=30_000))
    with pytest.raises(SourceError, match="two scrape"):
        await svc.query_distribution("x_bucket", start="now-2h", end="now-1h", step="30s")
    out = await svc.query_distribution("x_bucket", start="now-2h", end="now-1h")
    assert svc.datasets.meta(out["dataset"]).step_ms >= 60_000


async def test_unknown_source_is_explained(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError, match="unknown source"):
        await svc.query_distribution("x_bucket", source="nope")


async def test_heatmap_panel_data_merges_time_and_keeps_every_count(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution(
        "lat_bucket", by=["instance"], start="now-6h", end="now-1h", step="1m"
    )
    res = svc.show(out["dataset"], "How is latency distributed over time?")
    assert res.panel.spec["layers"][0]["mark"] == "heatmap"
    data = svc.panel_data(res.panel.id, width_px=100)  # 301 columns -> <= 50
    assert data["kind"] == "heatmap"
    assert data["effective_step_ms"] == 7 * 60_000
    assert data["facet_height_px"] == 140
    s = {x["labels"]["instance"]: x for x in data["series"]}["i0"]
    assert sum(s["n"]) == 100 * 301 and sum(s["cells"]["c"]) == 100 * 301
    assert sum(s["cover"]) == 301
    assert len(s["ts"]) <= 50


async def test_time_panels_report_their_kind(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("up", "now-2h", "now-1h", step="1m")
    assert svc.panel_data(svc.show(out["dataset"], "Up?").panel.id, 800)["kind"] == "time"


# test_service_distribution.py (append)
async def test_quantile_datasets_remember_their_histogram(tmp_path):
    svc = make_service(tmp_path)
    q = await svc.query(
        'histogram_quantile(0.95, sum by (le, instance) (rate(lat_seconds_bucket{job="a"}[5m])))',
        "now-2h", "now-1h", step="1m",
    )  # fmt: skip
    assert svc.datasets.meta(q["dataset"]).histogram == {
        "selector": 'lat_seconds_bucket{job="a"}', "by": ["instance"]
    }  # fmt: skip


async def test_histogram_panel_sums_whole_columns_in_window(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution(
        "lat_bucket", by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    meta = svc.datasets.meta(out["dataset"])
    a = meta.start_ms + 10 * 60_000 + 5_000
    res = svc.show(out["dataset"], "How is latency distributed at 10 past?", mark="histogram",
                   windows=[Window(start_ms=a, end_ms=a + 120_000, label="sel")])  # fmt: skip
    data = svc.panel_data(res.panel.id, 600)
    assert data["kind"] == "histogram" and data["mark"] == "histogram"
    w = {s["labels"]["instance"]: s["windows"][0] for s in data["series"]}["i1"]
    assert w["columns"] == 3 and w["n"] == 3 * 200
    assert (w["start_ms"], w["end_ms"]) == (
        meta.start_ms + 10 * 60_000,
        meta.start_ms + 13 * 60_000,
    )
    assert w["c"] == [3 * 180.0, 3 * 18.0, 3 * 2.0] and w["hi"][0] == 0.1


async def test_histogram_marks_are_validated(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_bucket", start="now-2h", end="now-1h", step="1m")
    with pytest.raises(ChartRejected, match="windows"):
        svc.show(out["dataset"], "q?", mark="ecdf", windows=[])
    plain = await svc.query("up", "now-2h", "now-1h", step="1m")
    with pytest.raises(ChartRejected, match="distribution"):
        svc.show(plain["dataset"], "q?", mark="heatmap")
    meta = svc.datasets.meta(out["dataset"])
    with pytest.raises(ValueError, match="outside"):
        svc.show(out["dataset"], "q?", mark="histogram",
                 windows=[Window(start_ms=meta.end_ms + 1, end_ms=meta.end_ms + 60_000)])  # fmt: skip


async def test_distribution_from_a_quantile_panel_fetches_its_histogram(tmp_path):
    src = FakeSource(name="default")  # dataset.source is looked up by registry name
    svc = make_service(tmp_path, src)
    q = await svc.query(
        'histogram_quantile(0.95, sum by (le, instance) (rate(lat_seconds_bucket{job="a"}[5m])))',
        "now-2h", "now-1h", step="1m",
    )  # fmt: skip
    panel = svc.show(q["dataset"], "p95 by instance?").panel
    m = svc.datasets.meta(q["dataset"])
    new = await svc.distribution_panel(panel.id, m.start_ms + 30 * 60_000, m.start_ms + 35 * 60_000)
    layer = new.spec["layers"][0]
    assert layer["mark"] == "histogram"
    assert [w["label"] for w in layer["windows"]] == ["selection", "previous"]
    assert src.hist_selectors == ['lat_seconds_bucket{job="a"}']
    assert "distributed between" in new.question


async def test_distribution_from_a_plain_panel_is_refused(tmp_path):
    svc = make_service(tmp_path)
    q = await svc.query("up", "now-2h", "now-1h", step="1m")
    panel = svc.show(q["dataset"], "Up?").panel
    with pytest.raises(SourceError, match="histogram"):
        await svc.distribution_panel(panel.id, 0, 60_000)


async def test_histogram_buckets_drawn_as_lines_are_flagged(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("sum by (le) (rate(x_bucket[5m]))", "now-2h", "now-1h", step="1m")
    assert "histogram_as_lines" in out["summary"]["caveats"]
    panel = svc.show(out["dataset"], "Buckets over time?").panel
    assert "histogram_as_lines" in svc.panel_data(panel.id, 600)["caveats"]
