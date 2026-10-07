"""The service on an Elasticsearch source: query / query_distribution dispatch (no PromQL)."""

import json

import pytest

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.sources.base import SourceError

from .fakes import FakeEsSource, make_service

QS = {"query_string": {"query": "service.name:checkout"}}
RATE = json.dumps({"query": QS})
STATS = json.dumps({"query": QS, "aggs": {"lat": {"stats": {"field": "event.duration"}}}})
P99 = json.dumps({"query": QS, "aggs": {"p": {"percentiles": {"field": "event.duration",
                                                             "percents": [99]}}}})  # fmt: skip
HIST = json.dumps({"query": QS, "aggs": {"lat": {"histogram": {"field": "event.duration",
                                                              "interval": 25}}}})  # fmt: skip


@pytest.fixture
def svc_es(tmp_path):
    svc = make_service(tmp_path)
    es = FakeEsSource()
    svc.sources.attach("es", es)
    return svc, es


async def test_a_rate_query_is_a_bucket_agg_dataset_in_es_dsl(svc_es):
    svc, es = svc_es
    out = await svc.query(RATE, start="now-1h", end="now", step="1m", source="es")
    meta = svc.datasets.meta(out["dataset"])
    assert (meta.representation, meta.query_language, meta.expr) == ("bucket_agg", "es_dsl", RATE)
    assert "zero_is_no_documents" in meta.source_caveats
    assert {c for c in es.calls} == {("fetch", RATE)}
    assert out["summary"]["series_count"] == 1


async def test_stats_has_no_zero_caveat(svc_es):
    svc, _ = svc_es
    out = await svc.query(STATS, start="now-1h", step="1m", source="es")
    assert "zero_is_no_documents" not in svc.datasets.meta(out["dataset"]).source_caveats


async def test_a_percentile_is_a_quantile_dataset_read_per_query_bucket(svc_es):
    svc, es = svc_es
    out = await svc.query(P99, start="now-1h", step="1m", source="es")
    meta = svc.datasets.meta(out["dataset"])
    assert (meta.representation, meta.quantile, meta.n_min) == ("quantile", 0.99, min_samples(0.99))
    assert "approximate_percentile" in meta.source_caveats
    assert {m for m, _ in es.calls} == {"fetch_values"}


async def test_a_histogram_query_points_at_query_distribution(svc_es):
    svc, es = svc_es
    with pytest.raises(SourceError, match="distribution") as e:
        await svc.query(HIST, start="now-1h", step="1m", source="es")
    assert "query_distribution" in e.value.hint and es.calls == []


async def test_an_unsupported_expr_is_a_source_error(svc_es):
    svc, _ = svc_es
    with pytest.raises(SourceError, match="not JSON"):
        await svc.query("rate(http_requests_total[5m])", start="now-1h", source="es")


async def test_a_step_finer_than_the_source_accepts_is_refused(svc_es):
    svc, _ = svc_es
    with pytest.raises(SourceError, match="finer than"):
        await svc.query(RATE, start="now-1h", step="5s", source="es")


async def test_query_distribution_steps_down_to_the_finest_query_step(svc_es):
    svc, es = svc_es
    # a PromQL source needs >= 2 series intervals per step (increase()); documents need none
    out = await svc.query_distribution(HIST, start="now-1h", step="15s", source="es")
    meta = svc.datasets.meta(out["dataset"])
    assert (meta.representation, meta.query_language) == ("distribution", "es_dsl")
    s = out["summary"]
    assert (
        s["buckets"] == "fixed-width buckets of 25 (offset 0), chosen by the query; each [lo, hi)"
    )
    assert s["counts"].startswith("doc_count per step")
    assert "smaller interval" in s["bucket_resolution"]
    assert "query_chosen_buckets" in s["caveats"]
    assert es.calls == [("fetch_histogram", HIST)]


async def test_fraction_over_on_query_chosen_buckets_is_at_or_above(svc_es):
    svc, _ = svc_es
    out = await svc.query_distribution(HIST, start="now-1h", step="1m", source="es")
    f = svc.fraction_over(out["dataset"], 25.0)
    assert f["compare"] == ">="
    [s] = f["series"]
    assert s["exact"] is True and s["fraction"] == pytest.approx(0.25)


async def test_prometheus_distributions_keep_greater_than(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query_distribution("lat_seconds_bucket", start="now-1h", step="1m")
    assert svc.fraction_over(out["dataset"], 0.1)["compare"] == ">"


async def test_promql_queries_are_unchanged_and_tagged_promql(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("up", start="now-1h", step="1m")
    assert svc.datasets.meta(out["dataset"]).query_language == "promql"


async def test_tool_descriptions_document_the_es_forms(tmp_path):
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp

    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    q = tools["query"].description or ""
    assert "Elasticsearch" in q and "percentiles" in q and "value_count" in q
    d = tools["query_distribution"].description or ""
    assert "Elasticsearch" in d and "[lo, hi)" in d
    assert ">=" in (tools["fraction_over"].description or "")
