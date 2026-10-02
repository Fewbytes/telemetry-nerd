import json
import math

import pytest

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
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


async def test_percentile_view_carries_source_buckets_per_column(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution(
        "lat_bucket", by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    res = svc.show(
        out["dataset"], "How did p50 and p99 move?", mark="percentiles", quantiles=[0.5, 0.99]
    )
    assert res.panel.spec["layers"][0]["quantiles"] == [0.5, 0.99]
    data = svc.panel_data(res.panel.id, width_px=800)  # 61 columns, no time LOD
    assert data["kind"] == "heatmap" and data["mark"] == "percentiles"
    i1 = {s["labels"]["instance"]: s for s in data["series"]}["i1"]  # 180/18/2 per column, n=200
    assert i1["quantiles"]["0.5"]["lo"][0] == -math.inf and i1["quantiles"]["0.5"]["hi"][0] == 0.1
    assert "0.99" not in i1["quantiles"]  # n=200 < 1000 in every 1m column
    assert len(i1["quantiles"]["0.9"]["ts"]) == 61
    # zoomed out: 7m columns sum counts (additive), n=1400 >= 1000, p99 is in (0.1, 1]
    wide = svc.panel_data(res.panel.id, width_px=20)
    p99 = {s["labels"]["instance"]: s for s in wide["series"]}["i1"]["quantiles"]["0.99"]
    assert p99["ts"] and set(zip(p99["lo"], p99["hi"], strict=True)) == {(0.1, 1.0)}


async def test_cumulative_views_get_source_buckets_when_bars_are_merged(tmp_path):
    cum = {f"{2.0**k:g}": 5.0 * (k + 1) for k in range(20)} | {"+Inf": 100.0}
    svc = make_service(tmp_path, FakeSource(cumulative=cum))
    out = await svc.query_distribution("lat_bucket", start="now-2h", end="now-1h", step="1m")
    meta = svc.datasets.meta(out["dataset"])
    w0 = Window(start_ms=meta.start_ms, end_ms=meta.start_ms + 600_000, label="w")
    res = svc.show(out["dataset"], "How heavy is the tail?", mark="ccdf", windows=[w0])
    data = svc.panel_data(res.panel.id, width_px=12)  # 4 bars max -> merged
    assert data["mark"] == "ccdf" and data["value_merge"] > 1
    w = data["series"][0]["windows"][0]
    assert len(w["source"]["c"]) > len(w["c"])
    assert sum(w["source"]["c"]) == sum(w["c"]) == w["n"]


async def test_unmerged_windows_have_no_source_copy(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_bucket", start="now-2h", end="now-1h", step="1m")
    meta = svc.datasets.meta(out["dataset"])
    res = svc.show(
        out["dataset"],
        "q?",
        mark="quantile_curve",
        windows=[Window(start_ms=meta.start_ms, end_ms=meta.end_ms)],
    )
    assert "source" not in svc.panel_data(res.panel.id, 600)["series"][0]["windows"][0]


async def test_fraction_over_is_exact_at_edges_and_bounded_inside(tmp_path):
    svc = make_service(tmp_path)  # per column: i0 90/9/1 (n=100), i1 180/18/2 (n=200)
    out = await svc.query_distribution(
        "lat_bucket", by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    [row] = svc.fraction_over(out["dataset"], 1.0)["series"]  # merged: 3 of 300 above 1.0
    assert row["exact"] and row["fraction"] == pytest.approx(0.01)
    assert row["ci95"][0] < 0.01 < row["ci95"][1]
    inside = svc.fraction_over(out["dataset"], 0.5)["series"][0]
    assert not inside["exact"] and inside["inside_bucket"] == [0.1, 1.0]
    lo, hi = inside["fraction"]
    assert lo == pytest.approx(0.01) and hi == pytest.approx(0.1)  # 3 above, +27 inside (0.1, 1]
    assert (
        inside["evidence"]["kind"] == "statistic" and inside["evidence"]["name"] == "fraction_over"
    )
    each = svc.fraction_over(out["dataset"], 1.0, by_series=True)
    assert len(each["series"]) == 2
    meta = svc.datasets.meta(out["dataset"])
    start = str(meta.start_ms + 60_000)
    short = svc.fraction_over(out["dataset"], 1.0, start=start, end=str(meta.start_ms + 180_000))
    assert short["series"][0]["steps"] == 2 and short["series"][0]["n"] == 600


async def test_fraction_over_refuses_a_bad_window(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_bucket", start="now-2h", end="now-1h", step="1m")
    with pytest.raises(ValueError, match="after"):
        svc.fraction_over(out["dataset"], 1.0, start="now-1h", end="now-2h")


class HoleyHistSource(FakeSource):
    async def fetch_histogram(self, selector, by, rng, step_ms):
        self.calls += 1
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        hole = set(ts[2:4])
        result = [
            {"metric": {"instance": f"i{k}", "le": le},
             "values": [[t / 1000, str(c * (k + 1))] for t in ts if not (k == 1 and t in hole)]}
            for k in range(self.n_series) for le, c in self.cumulative.items()
        ]  # fmt: skip
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))


async def test_heatmap_series_carry_column_state(tmp_path):
    svc = make_service(tmp_path, HoleyHistSource())
    out = await svc.query_distribution(
        "lat_bucket", by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    data = svc.panel_data(svc.show(out["dataset"], "Dist?").panel.id, width_px=4000)
    states = {s["labels"]["instance"]: s["state"] for s in data["series"]}
    assert states["i0"] is None
    assert states["i1"]["state"].count(2) == 2  # State.EMPTY


async def test_histogram_windows_report_column_coverage(tmp_path):
    svc = make_service(tmp_path, HoleyHistSource())
    out = await svc.query_distribution(
        "lat_bucket", by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    meta = svc.datasets.meta(out["dataset"])
    res = svc.show(out["dataset"], "Dist?", mark="histogram",
                   windows=[Window(start_ms=meta.start_ms, end_ms=meta.start_ms + 6 * 60_000,
                                   label="w")])  # fmt: skip
    data = svc.panel_data(res.panel.id, 600)
    w = {s["labels"]["instance"]: s["windows"][0] for s in data["series"]}
    assert w["i1"]["columns"] < w["i1"]["expected_columns"]
    assert w["i1"]["unknown"] is False
    assert w["i0"]["columns"] == w["i0"]["expected_columns"]
