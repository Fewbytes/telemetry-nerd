import httpx
import pytest
import respx

from telemetry_nerd.sources.base import Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.promql import PromQLSource

BASE = "http://prom.test"


def ok(data):
    return httpx.Response(200, json={"status": "success", "data": data})


def route(path):
    return respx.get(host="prom.test", path=path)


NAMES = ["up", "reqs_total", "lat_bucket", "lat_sum", "lat_count", "native_lat", "mystery"]
META = {
    "up": [{"type": "gauge", "help": "alive", "unit": ""}],
    "reqs_total": [{"type": "counter", "help": "requests", "unit": ""}],
    "lat_bucket": [{"type": "histogram", "help": "latency", "unit": "seconds"}],
    "native_lat": [{"type": "histogram", "help": "native latency", "unit": ""}],
}


def mock_all(names=NAMES, meta=META, labels=("__name__", "job"), tsdb=None):
    route("/api/v1/label/__name__/values").mock(return_value=ok(names))
    route("/api/v1/metadata").mock(return_value=ok(meta))
    route("/api/v1/labels").mock(return_value=ok(list(labels)))
    if tsdb is None:
        route("/api/v1/status/tsdb").mock(return_value=httpx.Response(404, text="nope"))
    else:
        route("/api/v1/status/tsdb").mock(return_value=ok(tsdb))


@respx.mock
async def test_discover_collects_names_metadata_labels_and_cardinality():
    mock_all(tsdb={"seriesCountByMetricName": [{"name": "up", "value": 40}]})
    d = await PromQLSource("p", BASE, flavor="prometheus").discover()
    by = {m.name: m for m in d.metrics}
    assert set(by) == {*NAMES, "lat"}  # the classic histogram's base is catalogued (6gp)
    assert (by["lat"].type, by["lat"].unit) == ("histogram", "seconds")
    assert (by["up"].type, by["up"].help, by["up"].unit) == ("gauge", "alive", None)
    assert by["lat_bucket"].unit == "seconds"
    assert by["mystery"].type is None  # no metadata is unknown, not "untyped"
    assert d.label_names == ("__name__", "job")
    assert d.cardinality == {"up": 40}
    assert d.origin == "source-metadata"
    assert "cardinality_top_only" in d.caveats
    assert d.metadata_coverage == pytest.approx(4 / 7)
    assert "metadata_coverage" in "".join(d.caveats)


@respx.mock
async def test_metadata_union_across_nondeterministic_retries():
    route("/api/v1/label/__name__/values").mock(return_value=ok(["a", "b", "c"]))
    route("/api/v1/labels").mock(return_value=ok(["__name__"]))
    route("/api/v1/status/tsdb").mock(return_value=httpx.Response(404))
    pages = [
        {"a": [{"type": "gauge", "help": "A", "unit": ""}]},
        {"b": [{"type": "counter", "help": "B", "unit": ""}]},
        {
            "a": [{"type": "gauge", "help": "A", "unit": ""}],
            "c": [{"type": "gauge", "help": "C", "unit": ""}],
        },
    ]
    route("/api/v1/metadata").mock(side_effect=[ok(p) for p in pages])
    d = await PromQLSource("p", BASE).discover()
    assert {m.name: m.type for m in d.metrics} == {"a": "gauge", "b": "counter", "c": "gauge"}
    assert d.metadata_coverage == 1.0
    assert not any(c.startswith("metadata_coverage") for c in d.caveats)


@respx.mock
async def test_native_vs_classic_histograms():
    mock_all()
    d = await PromQLSource("p", BASE).discover()
    assert d.histograms == {"lat": "classic", "native_lat": "native"}


@respx.mock
async def test_cardinality_unavailable_is_a_caveat_not_a_failure():
    mock_all()
    d = await PromQLSource("p", BASE).discover()
    assert d.cardinality is None
    assert "cardinality_unavailable" in d.caveats
    assert d.partial is True


@respx.mock
async def test_max_metrics_cap_reports_partial():
    mock_all(names=[f"m{i}" for i in range(10)], meta={})
    d = await PromQLSource("p", BASE, limits=Limits(max_metrics=4)).discover()
    assert len(d.metrics) == 4
    assert d.partial is True
    assert any(c.startswith("metrics_truncated") for c in d.caveats)


@respx.mock
async def test_metadata_failure_degrades_to_caveat():
    route("/api/v1/label/__name__/values").mock(return_value=ok(["a"]))
    route("/api/v1/labels").mock(return_value=ok(["__name__"]))
    route("/api/v1/status/tsdb").mock(return_value=httpx.Response(404))
    route("/api/v1/metadata").mock(return_value=httpx.Response(500))
    d = await PromQLSource("p", BASE).discover()
    assert [m.name for m in d.metrics] == ["a"]
    assert d.metrics[0].type is None
    assert "metadata_unavailable" in d.caveats and d.partial is True


@respx.mock
async def test_names_failure_raises():
    route("/api/v1/label/__name__/values").mock(return_value=httpx.Response(500))
    with pytest.raises(SourceUnavailable):
        await PromQLSource("p", BASE).discover()


@respx.mock
async def test_names_error_status_raises_source_error():
    route("/api/v1/label/__name__/values").mock(
        return_value=httpx.Response(200, json={"status": "error", "error": "denied"})
    )
    with pytest.raises(SourceError, match="denied"):
        await PromQLSource("p", BASE).discover()


def series(*ts_s):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"__name__": "m"}, "values": [[t, "1"] for t in ts_s]}],
        },
    }


@respx.mock
async def test_scrape_interval_is_the_median_sample_spacing():
    respx.get(host="prom.test", path="/api/v1/query").mock(
        return_value=httpx.Response(200, json=series(0, 15, 30, 46, 60, 75))
    )
    assert await PromQLSource("p", BASE).scrape_interval("m") == 15_000


@respx.mock
async def test_scrape_interval_none_when_sparse_or_absent():
    respx.get(host="prom.test", path="/api/v1/query").mock(
        return_value=httpx.Response(200, json=series(0, 15))
    )
    assert await PromQLSource("p", BASE).scrape_interval("m") is None
    respx.get(host="prom.test", path="/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )
    )
    assert await PromQLSource("p", BASE).scrape_interval("m") is None
