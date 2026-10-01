import json

import pytest

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
