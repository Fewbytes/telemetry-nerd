"""check_littles_law with arrivals and latency from an Elasticsearch source and concurrency from
a Prometheus source (cross-source; spec: Little's law on an ES source)."""

import asyncio
import json

import pytest

from .fakes import NOW, make_service
from .littles_sim import CONCURRENCY, IN_FLIGHT_FIELD, EsSimSource, SimSource, simulate

START = NOW - 3_600_000
QS = {"query_string": {"query": "service.name:checkout"}}
ARRIVALS = json.dumps({"query": QS})
LATENCY = json.dumps({"query": QS, "aggs": {"lat": {"stats": {"field": "event.duration"}}}})
P99 = json.dumps({"query": QS, "aggs": {"p": {"percentiles": {"field": "event.duration",
                                                             "percents": [99]}}}})  # fmt: skip


def _setup(tmp_path):
    sims = {"i0": simulate(20, rates=[(0.0, 6.0)], c=10)}
    es = EsSimSource(sims, START)  # registered as "default" by make_service
    prom = SimSource(sims, START)
    prom.name, prom.identity = "prom", "prom-sim"
    svc = make_service(tmp_path, source=es)
    svc.sources.attach("prom", prom)
    return svc, es, prom


def _run(svc, **kw):
    args = {"arrival_rate": ARRIVALS, "latency": LATENCY, "concurrency": CONCURRENCY,
            "concurrency_source": "prom", "latency_unit": "s"}  # fmt: skip
    return asyncio.run(
        svc.check_littles_law(start="now-1h", end="now", window="5m", **{**args, **kw})
    )


def test_es_arrivals_and_latency_with_a_prometheus_gauge_are_consistent(tmp_path):
    svc, es, prom = _setup(tmp_path)
    out = _run(svc)
    assert out["verdict"] == "consistent"
    ds = out["datasets"]
    assert ds["latency_sum"] == ds["latency_count"]  # one stats dataset feeds both roles
    assert set(es.exprs) == {ARRIVALS, LATENCY}  # (the series cache may split a fetch in chunks)
    assert all(CONCURRENCY in e for e in prom.exprs)
    assert prom.probes and not es.probes  # the gauge's series interval is probed on its source
    assumptions = {a["name"]: a for a in out["assumptions"]}
    assert assumptions["sources"]["status"] == "assumed"
    assert "documents" in assumptions["sources"]["detail"]
    assert "concurrency from prom" in assumptions["sources"]["detail"]
    assert "[t − step, t)" in assumptions["window_alignment"]["detail"]
    assert assumptions["label_sets"]["status"] == "ok"


def test_label_sets_flags_when_arrival_rate_and_latency_queries_differ(tmp_path):
    """_es_roles used to discard arrival_rate's parsed query, so the check never compared it
    against latency's: two ES queries filtering different services would still claim "the same
    selectors". With both kept, a real mismatch is now flagged by name."""
    svc, _, _ = _setup(tmp_path)
    other_qs = {"query_string": {"query": "service.name:search"}}
    other_arrivals = json.dumps({"query": other_qs})
    out = _run(svc, arrival_rate=other_arrivals)
    label_sets = {a["name"]: a for a in out["assumptions"]}["label_sets"]
    assert label_sets["status"] == "flagged"
    assert "arrival_rate and latency queries differ" in label_sets["detail"]
    assert str(other_qs) in label_sets["detail"] and str(QS) in label_sets["detail"]


IN_FLIGHT = json.dumps({"query": QS, "aggs": {"l": {"stats": {"field": IN_FLIGHT_FIELD}}}})


def test_concurrency_can_come_from_an_es_query(tmp_path):
    """Any source can serve L: here the same ES index gives arrivals, latency and the in-flight
    level as a stats aggregation (its mean per step)."""
    svc, es, _ = _setup(tmp_path)
    out = _run(svc, concurrency=IN_FLIGHT, concurrency_source="default")
    assert out["verdict"] == "consistent"
    assert set(es.exprs) == {ARRIVALS, LATENCY, IN_FLIGHT}


def test_an_es_stats_gauge_is_read_as_a_bucket_mean_not_trapezoid_corrected(tmp_path):
    """sgb.12: the stats gauge is the mean of the documents in each query bucket, so it is not
    given the end-of-interval (trapezoid) correction a scraped gauge gets; the documents'
    position in the bucket is a bias bound, and the assumption says so."""
    svc, _, _ = _setup(tmp_path)
    es_gauge = _run(svc, concurrency=IN_FLIGHT, concurrency_source="default")
    align = {a["name"]: a for a in es_gauge["assumptions"]}["window_alignment"]
    assert align["status"] == "assumed"
    assert "mean of the gauge documents" in align["detail"] and "evenly in time" in align["detail"]
    prom_gauge = _run(svc)
    align = {a["name"]: a for a in prom_gauge["assumptions"]}["window_alignment"]
    assert align["status"] == "ok" and "corrected (trapezoid)" in align["detail"]


def test_es_concurrency_with_a_prometheus_signal_source_is_consistent(tmp_path):
    svc, es, _ = _setup(tmp_path)
    out = _run(
        svc,
        source="prom",
        arrival_rate="http_server_requests_total",
        latency="http_server_duration_seconds",
        concurrency=IN_FLIGHT,
        concurrency_source="default",
    )
    assert out["verdict"] == "consistent"
    assert es.exprs == [IN_FLIGHT]


def test_es_concurrency_must_be_the_stats_form(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="stats aggregation on the in-flight"):
        _run(svc, concurrency=ARRIVALS, concurrency_source="default")


def test_grouping_with_an_es_concurrency_query_is_refused(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="one total"):
        _run(svc, concurrency=IN_FLIGHT, concurrency_source="default", by=["service"])


def _setup_two(tmp_path, hide=frozenset()):
    sims = {i: simulate(seed, rates=[(0.0, 3.0)], c=6) for i, seed in (("i0", 21), ("i1", 22))}
    es = EsSimSource(sims, START)
    prom = SimSource(sims, START, hide=hide)
    prom.name, prom.identity = "prom", "prom-sim"
    svc = make_service(tmp_path, source=es)
    svc.sources.attach("prom", prom)
    return svc, es, prom


def test_grouping_across_sources_maps_an_es_field_to_a_prometheus_label(tmp_path):
    """by=["host.name=instance"]: the ES queries are grouped by the field (a terms aggregation
    the check adds), the gauge summed by the label, and groups join on equal values."""
    svc, es, prom = _setup_two(tmp_path)
    out = _run(svc, by=["host.name=instance"])
    assert out["verdict"] == "consistent"
    groups = {g["labels"]["host.name"]: g for g in out["groups"]}
    assert set(groups) == {"i0", "i1"}
    assert all(g["verdict"] == "consistent" for g in groups.values())
    grouped = [json.loads(e) for e in es.exprs]
    assert all(e["aggs"]["littles_by"]["terms"]["field"] == "host.name" for e in grouped)
    assert any("stats" in e["aggs"]["littles_by"].get("aggs", {}).get("lat", {}) for e in grouped)
    assert all("sum by (instance)" in e for e in prom.exprs)
    label_sets = {a["name"]: a for a in out["assumptions"]}["label_sets"]
    assert label_sets["status"] == "assumed"
    assert "host.name = instance" in label_sets["detail"]
    assert "unmatched" not in out


def test_a_group_missing_on_the_concurrency_source_is_unmatched(tmp_path):
    svc, _, _ = _setup_two(tmp_path, hide={("gauge", "i1")})
    out = _run(svc, by=["host.name=instance"])
    [u] = out["unmatched"]
    assert u["labels"] == {"host.name": "i1"} and u["missing_in"] == ["concurrency"]
    label_sets = {a["name"]: a for a in out["assumptions"]}["label_sets"]
    assert label_sets["status"] == "flagged"


def test_an_unmapped_es_field_is_refused_with_the_mapping_hint(tmp_path):
    svc, _, _ = _setup_two(tmp_path)
    with pytest.raises(ValueError, match=r"host.name=instance"):
        _run(svc, by=["host.name"])


def test_a_mapping_on_one_source_is_refused(tmp_path):
    svc, _, _ = _setup_two(tmp_path)
    with pytest.raises(ValueError, match="one source"):
        _run(svc, source="prom", arrival_rate="http_requests_total",
             latency="http_request_duration_seconds", concurrency_source="prom",
             by=["instance=instance"])  # fmt: skip


def test_terms_grouping_written_in_the_query_points_at_by(tmp_path):
    svc, _, _ = _setup_two(tmp_path)
    grouped = json.dumps({"query": QS, "aggs": {"t": {"terms": {"field": "host.name"}}}})
    with pytest.raises(ValueError, match="by="):
        _run(svc, arrival_rate=grouped)


def test_es_grouped_query_shapes():
    from telemetry_nerd.core.littles_ops import ES_GROUP_SIZE, _es_grouped

    one = json.loads(_es_grouped(ARRIVALS, ["host.name"]))
    assert one == {"query": QS, "aggs": {"littles_by": {
        "terms": {"field": "host.name", "size": ES_GROUP_SIZE}}}}  # fmt: skip
    two = json.loads(_es_grouped(LATENCY, ["host.name", "url.path"]))
    comp = two["aggs"]["littles_by"]
    assert [list(s) for s in comp["composite"]["sources"]] == [["host.name"], ["url.path"]]
    assert comp["aggs"] == {"lat": {"stats": {"field": "event.duration"}}}


def test_a_percentile_latency_is_refused(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="looks like a percentile"):
        _run(svc, latency=P99)


def test_arrival_rate_must_be_the_rate_form(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="rate form"):
        _run(svc, arrival_rate=LATENCY)


def test_a_binding_is_refused_on_an_es_source(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="binding"):
        _run(svc, binding="checkout")


def test_es_latency_field_suffix_is_not_inferred_as_a_unit(tmp_path):
    """_unit used to fall back to catalog_facts, which infers a unit from a Prometheus metric
    name suffix (e.g. _seconds). An ES field path like event.duration_seconds is not a metric
    name, and the design spec says name-based inference is skipped for ES: with no catalog claim
    for the field, the unit must come back unknown/assumed, not 's' guessed from the name."""
    svc, _, _ = _setup(tmp_path)
    suffixed_qs = {"query_string": {"query": "service.name:checkout"}}
    suffixed_latency = json.dumps(
        {"query": suffixed_qs, "aggs": {"lat": {"stats": {"field": "event.duration_seconds"}}}}
    )
    out = _run(svc, latency=suffixed_latency, latency_unit=None)
    units = {a["name"]: a for a in out["assumptions"]}["units"]
    assert units["status"] == "assumed"
    assert "ASSUMED seconds" in units["detail"]
    assert "latency_unit_assumed" in out["caveats"]


async def test_check_littles_law_documents_concurrency_source(tmp_path):
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp

    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    doc = tools["check_littles_law"].description or ""
    assert "concurrency_source" in doc and "Elasticsearch" in doc


def test_two_prometheus_sources_map_label_names(tmp_path):
    """The mapping is not ES-specific: signals on one PromQL source grouped by `instance`, the
    gauge on another whose label is `pod`."""
    sims = {i: simulate(seed, rates=[(0.0, 3.0)], c=6) for i, seed in (("i0", 21), ("i1", 22))}
    sig = SimSource(sims, START)
    gauge = SimSource(sims, START, label="pod")
    gauge.name, gauge.identity = "gauges", "gauge-sim"
    svc = make_service(tmp_path, source=sig)
    svc.sources.attach("gauges", gauge)
    out = _run(svc, arrival_rate="http_requests_total", latency="http_request_duration_seconds",
               concurrency_source="gauges", by=["instance=pod"])  # fmt: skip
    assert {g["labels"]["instance"] for g in out["groups"]} == {"i0", "i1"}
    assert all("sum by (pod)" in e for e in gauge.exprs)
    assert all("sum by (instance)" in e for e in sig.exprs)
