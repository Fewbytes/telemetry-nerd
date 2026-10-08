"""check_littles_law with arrivals and latency from an Elasticsearch source and concurrency from
a Prometheus source (cross-source; spec: Little's law on an ES source)."""

import asyncio
import json

import pytest

from .fakes import NOW, make_service
from .littles_sim import CONCURRENCY, EsSimSource, SimSource, simulate

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


def test_concurrency_is_never_read_from_an_es_source(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="PromQL source"):
        _run(svc, concurrency_source="default")


def test_grouping_across_two_sources_is_refused(tmp_path):
    svc, _, _ = _setup(tmp_path)
    with pytest.raises(ValueError, match="two sources"):
        _run(svc, by=["service"])


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
