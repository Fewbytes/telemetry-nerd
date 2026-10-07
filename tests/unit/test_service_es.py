"""The service on an Elasticsearch source: query / query_distribution dispatch (no PromQL)."""

import json

import pytest

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.sources.base import SourceError

from .es_fake import FakeEs, search_response, tb
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


async def test_a_wide_query_is_not_split_into_cache_chunks_so_a_boundary_zero_survives(tmp_path):
    """Before this fix, _query_es routed ES fetches through SeriesCache.get, which splits any
    requested range into fixed 720-bucket chunks and fetches each as its own Elasticsearch
    request: a real interior zero sitting at what would have been a chunk edge became a fake
    leading/trailing bucket for `_interior`'s zero-fill rule and was silently dropped instead of
    reported as 0/s. Bypassing the cache (src.fetch called directly) sends one request for the
    whole range, so the zero is read as a genuine observation and the request count no longer
    scales with the number of 720-bucket chunks the old cache would have used."""
    step_ms = 60_000
    chunk_span_ms = step_ms * 720  # the series cache's old chunk width (12h at a 1m step)
    zero_ts = chunk_span_ms  # would have been the trailing edge of the first old-style chunk

    def handle(body: dict) -> dict:
        window = body["query"]["bool"]["filter"][1]["range"]["@timestamp"]
        gte, lt = window["gte"], window["lt"]
        buckets = []
        t = gte + step_ms
        while t <= lt:
            buckets.append(tb(t - step_ms, 0 if t == zero_ts else 1))
            t += step_ms
        return search_response(buckets)

    fake = FakeEs(search=handle)
    svc = make_service(tmp_path)
    svc.sources.attach("es", fake.source())
    end_ms = chunk_span_ms * 2 + 60 * step_ms  # spans would-be chunks on both sides of the zero
    out = await svc.query(RATE, start="0", end=str(end_ms), step="1m", source="es")
    assert len(fake.searches()) == 1  # one request for the whole range, not one per old chunk
    _, result = svc.datasets.get(out["dataset"])
    row = next(r for r in result.buckets.to_pylist() if r["ts_ms"] == zero_ts)
    assert (row["avg"], row["count"]) == (0.0, 1)


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


from telemetry_nerd.model.discovery import Discovery, MetricInfo


def learn_duration_unit(svc, unit="ms"):
    svc.ws.catalog_learn("es", Discovery(
        metrics=(MetricInfo("event.duration", None, None, unit),), label_names=(), histograms={},
        cardinality=None, metadata_coverage=1.0, caveats=(), partial=False, naming="fields",
    ))  # fmt: skip


async def test_show_takes_the_unit_of_the_aggregated_field_from_the_catalog(svc_es):
    svc, _ = svc_es
    learn_duration_unit(svc)
    out = await svc.query(STATS, start="now-1h", step="1m", source="es")
    res = svc.show(out["dataset"], "How slow is checkout?")
    assert res.panel.spec["y"]["unit"] == "ms"
    assert "event.duration" in res.panel.spec["y"]["unit_provenance"]


async def test_show_leaves_rate_forms_unitless_and_accepts_a_given_unit(svc_es):
    svc, _ = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    assert svc.show(out["dataset"], "How busy is checkout?").panel.spec["y"].get("unit") is None
    given = svc.show(out["dataset"], "How busy is checkout?", unit="req/s")
    assert given.panel.spec["y"]["unit"] == "req/s"
    assert not [i for i in given.issues if i.rule == "raw_counter"]


async def test_show_auto_never_rewrites_an_es_query_as_a_rate(svc_es):
    svc, es = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    calls = len(es.calls)
    res = await svc.show_auto(out["dataset"], "How busy is checkout?")
    assert res.panel.spec.get("auto") is None and len(es.calls) == calls


async def test_operating_profiles_are_refused_on_es_sources(svc_es):
    svc, _ = svc_es
    with pytest.raises(SourceError, match="PromQL-only"):
        await svc.operating_profile(RATE, source="es")


async def test_the_normal_band_overlay_is_unavailable_with_a_reason(svc_es):
    svc, _ = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    panel = svc.show(out["dataset"], "How busy is checkout?").panel
    normal = svc.panel_data(panel.id, 800)["overlays"]["normal"]
    assert normal["available"] is False and "PromQL-only" in normal["reason"]


async def test_analyze_on_an_es_dataset_says_no_profile_can_shape_the_centre(svc_es):
    svc, _ = svc_es
    out = await svc.query(RATE, start="now-1h", step="1m", source="es")
    res = await svc.analyze_profiled(out["dataset"])
    assert res["seasonal_centre"]["status"] == "unavailable"
    assert "PromQL-only" in res["seasonal_centre"]["reason"]
