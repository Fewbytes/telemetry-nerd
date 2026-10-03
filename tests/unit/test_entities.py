"""Service/entity discovery (bead rbz): `entities` over an OTel-demo-shaped source, catalog search
by natural phrases, name groups in catalog_family, and absence-aware empty results."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp import Client

from telemetry_nerd.core.entity_ops import base_name, metric_matcher
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.promql import PromQLSource

from .demo_source import DemoSource
from .fakes import FakeSource, make_service


@pytest.fixture
async def demo(tmp_path):
    src = DemoSource()
    svc = make_service(tmp_path, src)
    mcp = build_mcp(svc, "http://x")
    async with Client(mcp) as c:
        await c.call_tool("source_learn", {"source": "default"})
    return svc, mcp, src


async def call(mcp, name, args=None):
    async with Client(mcp) as c:
        res = await c.call_tool(name, args or {})
    text = res.content[0].text
    return (text, True) if res.is_error else (json.loads(text), False)


# --- catalog search and families -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("query", "expect"),
    [
        ("http server request duration", "http_server_request_duration_seconds"),
        ("HTTP server latency", "http_server_request_duration_seconds"),
        ("http.server.request.duration", "http_server_request_duration_seconds"),
        ("rpc server call duration", "rpc_server_call_duration_seconds"),
        ("grpc server duration", "rpc_server_duration_milliseconds"),
        ("span metrics calls", "traces_span_metrics_calls_total"),
        ("payment transactions", "demo_payment_transactions_total"),
        ("inbound RPC", "rpc_server_call_duration_seconds"),  # description only
    ],
)
async def test_catalog_search_matches_natural_phrases(demo, query, expect):
    _, mcp, _ = demo
    out, err = await call(mcp, "catalog_search", {"query": query})
    assert not err
    assert expect in [r["metric"] for r in out["results"]], (query, out)
    assert "match" not in out  # every word matched


async def test_catalog_search_ranks_name_matches_and_marks_partial(demo):
    _, mcp, _ = demo
    out, _ = await call(mcp, "catalog_search", {"query": "http server request duration"})
    # the histogram base name and its series rank first; every row matched all words
    assert out["results"][0]["metric"].startswith("http_server_request_duration_seconds")
    assert out["terms"] == ["http", "server", "request", "duration"]
    part, _ = await call(mcp, "catalog_search", {"query": "payment transaction errors"})
    assert part["match"] == "partial" and part["results"]
    assert [r["metric"] for r in part["results"]] == ["demo_payment_transactions_total"]
    assert part["results"][0]["matched"] == "2/3" and "Check the names" in part["note"]


async def test_catalog_search_empty_result_says_what_was_searched(demo):
    _, mcp, _ = demo
    out, _ = await call(mcp, "catalog_search", {"query": "kafka consumer lag"})
    assert out["total"] == 0
    note = out["note"]
    assert "'default'" in note and "not evidence" in note and "entities" in note
    assert "names and descriptions of" in note


async def test_catalog_family_lists_a_name_group(demo):
    _, mcp, _ = demo
    learned, _ = await call(mcp, "source_learn", {"source": "default"})
    groups = {f["family"] for f in learned["families"]}
    assert {"rpc_server", "http_server", "traces_span"} <= groups
    assert "name_template_families" in learned and "entities" in learned["next"]
    out, err = await call(mcp, "catalog_family", {"family": "rpc_server"})
    assert not err and out["kind"] == "name_group"
    names = {r["metric"] for r in out["metrics"]}
    assert {"rpc_server_call_duration_seconds", "rpc_server_duration_milliseconds"} <= names
    # every group source_learn lists resolves to metrics
    for g in groups:
        got, _ = await call(mcp, "catalog_family", {"family": g})
        assert got.get("metrics"), g
    # unknown: an honest note, the lists of both kinds
    miss, err = await call(mcp, "catalog_family", {"family": "kafka_consumer"})
    assert not err and "not evidence" in miss["note"] and miss["name_groups"]
    # a name group cannot be confirmed or split
    text, err = await call(
        mcp, "catalog_family", {"family": "rpc_server", "action": "split", "basis": "x"}
    )
    assert err and "name group" in text


# --- entities ------------------------------------------------------------------------------------
async def test_entities_lists_every_service_with_its_families(demo):
    _, mcp, _ = demo
    out, err = await call(mcp, "entities", {"kind": "service"})
    assert not err, out
    assert out["primary_label"] == "service_name"
    by = {e["value"]: e for e in out["entities"]}
    assert {"payment", "checkout", "frontend", "cart", "email", "quote"} <= set(by)
    pay = by["payment"]
    # payment reports span metrics and runtime metrics, no http/rpc server metrics
    assert "traces_span" in pay["families"] and "http_server" not in pay["families"]
    assert "traces_span_metrics_calls_total" in pay["metrics"]
    assert "traces_span_metrics_duration_milliseconds" in pay["metrics"]  # base, not _bucket
    assert "RED:spanmetrics" in pay["bindings"] and "RED:otel_http" not in pay["bindings"]
    assert pay["next"] == 'binding_suggest(kind="RED", key="payment")'
    assert "RED:otel_http" in by["frontend"]["bindings"]
    # quote stopped reporting 20 minutes ago: listed, flagged silent
    assert by["quote"]["active_recent"] is False and pay["active_recent"] is True
    assert "silent" in out["coverage"]
    # the other identity labels are reported with their coverage
    labels = {lb["label"]: lb for lb in out["labels"]}
    assert labels["job"]["values"] >= 16  # the demo services plus the collector
    assert "app" in out["coverage"]["absent_labels"]
    assert "never 'does not exist'" in out["coverage"]["note"]


async def test_entities_narrowed_by_metric_and_suggest_by_key(demo):
    svc, mcp, _ = demo
    out, _ = await call(mcp, "entities", {"metric": "http_server_request_duration_seconds"})
    assert sorted(e["value"] for e in out["entities"]) == ["cart", "frontend", "shipping"]
    # the key the entity row points to gives the span-metrics RED binding for payment
    sug = svc.ws.binding_suggest("default", kind="RED", key="payment")
    top = sug["suggestions"][0]
    assert top["id"] == "RED:spanmetrics" and top["key"] == "payment"
    assert top["roles"]["duration"] == "traces_span_metrics_duration_milliseconds"


async def test_entities_is_cached_and_bounded(demo):
    _, mcp, src = demo
    await call(mcp, "entities", {})
    n = len(src.label_calls)
    again, _ = await call(mcp, "entities", {})
    assert again["cached"] is True and len(src.label_calls) == n
    small, _ = await call(mcp, "entities", {"limit": 3})
    assert len(small["entities"]) == 3 and "truncated" in small["coverage"]
    assert small["labels"][0]["truncated"] is True


async def test_entities_absent_label_is_not_absence(demo):
    _, mcp, _ = demo
    out, err = await call(mcp, "entities", {"kind": "namespace", "label": "k8s_namespace_name"})
    assert not err and out["entities"] == []
    assert "Absence of evidence is not evidence of absence" in out["note"]
    assert out["coverage"]["searched_labels"] == ["k8s_namespace_name"]


async def test_entities_refuses_bad_arguments_and_unsupported_sources(demo, tmp_path):
    _, mcp, _ = demo
    text, err = await call(mcp, "entities", {"kind": "planet"})
    assert err and "kind must be one of" in text
    text, err = await call(mcp, "entities", {"label": "a b"})
    assert err and "not a label name" in text
    (tmp_path / "p").mkdir()
    plain = build_mcp(make_service(tmp_path / "p", FakeSource(name="default")), "http://x")
    text, err = await call(plain, "entities", {})
    assert err and "cannot list label values" in text


def test_metric_matcher_and_base_name():
    assert base_name("x_seconds_bucket") == "x_seconds" and base_name("x_total") == "x_total"
    assert metric_matcher("a.b_bucket") == '{__name__=~"a\\.b(?:_bucket|_count|_sum)?"}'


async def test_promql_label_values_request_shape():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"status": "success", "data": ["b", "a", "c"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    src = PromQLSource("s", "http://vm", client=client)
    got = await src.label_values(
        "service_name", ['{__name__="x"}', '{job="y"}'], TimeRange(1_000, 61_000), limit=2
    )
    assert got == ["a", "b"]
    q = seen[0].url
    assert q.path == "/api/v1/label/service_name/values"
    assert q.params.get_list("match[]") == ['{__name__="x"}', '{job="y"}']
    assert (q.params["start"], q.params["end"], q.params["limit"]) == ("1.000", "61.000", "2")


async def test_empty_query_result_says_absence_of_evidence(demo):
    svc, _, _ = demo
    expr = 'sum by (service_name) (rate(http_server_request_duration_seconds_count{service_name="payment"}[1m]))'
    out = await svc.query(expr, start="now-1h", end="now", step="1m")
    s = out["summary"]
    assert s["series_count"] == 0 and "empty" in s["caveats"]
    assert "Absence of evidence is not evidence of absence" in s["empty_result"]
    assert "'default'" in s["empty_result"] and "`entities`" in s["empty_result"]
    # the same entity is found through the metrics it does report
    full = await svc.query(
        'sum by (service_name) (rate(traces_span_metrics_calls_total{service_name="payment"}[1m]))',
        start="now-1h", end="now", step="1m",
    )  # fmt: skip
    assert full["summary"]["series_count"] == 1 and "empty_result" not in full["summary"]
