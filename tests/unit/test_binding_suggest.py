import json
import random
from pathlib import Path

import pytest
from mcp import Client

from telemetry_nerd.catalog.binding_suggest import suggest_bindings
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import FakeSource, make_service

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "packs"


def real(name):
    return json.loads((FIX / name).read_text())


def infos(meta, native=()):
    return [
        MetricInfo(n, e[0]["type"], e[0]["help"] or None, e[0]["unit"] or None)
        for n, e in meta.items()
    ], {
        n: ("native" if n in native else "classic")
        for n, e in meta.items()
        if e[0]["type"] == "histogram"
    }


def discovery(*metas, extra=(), native=("traces_spanmetrics_latency",)):
    ms, hs = [], {}
    for m in metas:
        i, h = infos(m, native)
        ms += i
        hs |= h
    ms += list(extra)
    return Discovery(tuple(ms), (), hs, None, 1.0, (), False)


def synthetic(*specs):
    ms = [MetricInfo(n, t, None, None) for n, t in specs]
    return Discovery(
        tuple(ms), (), {n: "classic" for n, t in specs if t == "histogram"}, None, 1.0, (), False
    )


async def learned(tmp_path, disc):
    svc = make_service(tmp_path, FakeSource(name="default", discovery=disc))
    mcp = build_mcp(svc, "http://x")
    async with Client(mcp) as c:
        await c.call_tool("source_learn", {"source": "default"})
    return svc, mcp


async def call(mcp, name, args):
    async with Client(mcp) as c:
        res = await c.call_tool(name, args)
    return json.loads(res.content[0].text) if not res.is_error else res.content[0].text


def by_id(out):
    return {s["id"]: s for s in out["suggestions"]}


@pytest.fixture
async def play(tmp_path):
    d = discovery(
        real("play_otel_metadata.json"),
        real("play_k8s_metadata.json"),
        real("wikimedia_node_metadata.json"),
    )
    return await learned(tmp_path, d)


async def test_otel_http_red_and_littles_law(play):
    svc, _ = play
    got = by_id(svc.ws.binding_suggest("default", limit=100))
    red = got["RED:otel_http"]
    assert red["key"] == "http.server" and red["join_on"] == ["service_name"]
    assert red["roles"] == {
        "rate": "http_server_requests_total",
        "errors": "http_server_requests_total",
        "duration": "http_server_request_duration_seconds",
    }
    d = red["detail"]
    assert d["rate"]["form"] == "counter_rate" and "rate(" in d["rate"]["expr"]
    assert d["errors"]["form"] == "label_split" and "5.." in d["errors"]["expr"]
    # histogram for latency, never a percentile; the other duration generations are alternatives
    assert d["duration"]["form"] == "histogram" and "histogram_quantile" in d["duration"]["expr"]
    assert "_sum" in d["duration"]["expr"] and "_bucket" in d["duration"]["expr"]
    alts = {a["metric"] for a in d["duration"]["alternatives"]}
    assert alts == {"http_server_duration_seconds", "http_server_duration_milliseconds"}
    assert not d["duration"]["ambiguous"] and d["rate"]["ambiguous"]
    assert not red["unfilled"] and all(x["basis"] == ["pack"] for x in d.values())

    ll = got["littles_law:otel_http"]
    assert ll["roles"] == {
        "arrival_rate": "http_server_requests_total",
        "latency": "http_server_request_duration_seconds",
        "concurrency": "http_server_active_requests",
    }
    # arrival rate can also come from the histogram's own count
    assert "http_server_request_duration_seconds" in {
        a["metric"] for a in ll["detail"]["arrival_rate"]["alternatives"]
    }


async def test_rpc_and_spanmetrics_red(play):
    svc, _ = play
    got = by_id(svc.ws.binding_suggest("default", "RED", limit=100))
    rpc = got["RED:otel_rpc"]
    assert rpc["roles"]["duration"] == "rpc_server_call_duration_seconds"
    assert (
        "_count" in rpc["detail"]["rate"]["expr"]
        and rpc["roles"]["rate"] == "rpc_server_call_duration_seconds"
    )
    sp = got["RED:spanmetrics"]
    assert sp["roles"] == {
        "rate": "traces_spanmetrics_calls_total",
        "errors": "traces_spanmetrics_calls_total",
        "duration": "traces_spanmetrics_latency",
    }
    assert "STATUS_CODE_ERROR" in sp["detail"]["errors"]["expr"]
    assert sp["detail"]["duration"]["histogram"] == "native"  # no _bucket members in the catalog
    assert "histogram_avg" in sp["detail"]["duration"]["expr"]
    assert sp["caveat"]


async def test_spanmetrics_littles_law_lists_the_missing_concurrency(play):
    svc, _ = play
    ll = by_id(svc.ws.binding_suggest("default", "littles_law", "spanmetrics"))[
        "littles_law:spanmetrics"
    ]
    assert ll["roles"]["concurrency"] is None
    (u,) = ll["unfilled"]
    assert u["role"] == "concurrency" and u["suggest_instrumentation"]["type"] == "gauge"
    assert "spanmetrics" in u["suggest_instrumentation"]["where"]
    assert (
        "histogram_count" in ll["detail"]["latency"]["expr"]
        or ll["detail"]["latency"]["histogram"] == "native"
    )


async def test_node_exporter_use_per_resource(play):
    svc, _ = play
    got = by_id(svc.ws.binding_suggest("default", "USE", limit=100))
    cpu = got["USE:node_cpu"]
    assert cpu["key"] == "node:cpu" and cpu["join_on"] == ["instance"]
    assert cpu["roles"] == {
        "utilization": "node_cpu_seconds_total",
        "saturation": "node_pressure_cpu_waiting_seconds_total",
        "errors": None,
    }
    assert 'mode="idle"' in cpu["detail"]["utilization"]["expr"]
    alts = [a["metric"] for a in cpu["detail"]["saturation"]["alternatives"]]
    assert alts[:2] == ["node_schedstat_waiting_seconds_total", "node_load1"]
    assert [u["role"] for u in cpu["unfilled"]] == ["errors"]
    # telemetry-nerd-012: an unfilled role names where (the entity/scope) to add it, even with
    # no code citation on file for a sibling metric of this scope
    where = cpu["unfilled"][0]["suggest_instrumentation"]["where"]
    assert "node:cpu" in where and "node_exporter" in where

    mem = got["USE:node_memory"]
    assert mem["roles"] == {
        "utilization": "node_memory_MemAvailable_bytes",
        "saturation": "node_pressure_memory_waiting_seconds_total",
        "errors": "node_vmstat_oom_kill",
    }
    assert "relation" in mem["detail"]["utilization"]["basis"]  # MemAvailable bounded_by MemTotal
    disk = got["USE:node_disk"]
    assert disk["roles"]["utilization"] == "node_disk_io_time_seconds_total"
    assert disk["roles"]["saturation"] == "node_disk_io_time_weighted_seconds_total"
    assert disk["join_on"] == ["instance", "device"]
    net = got["USE:node_network"]
    assert net["roles"]["errors"] in {
        "node_network_receive_errs_total",
        "node_network_transmit_errs_total",
    }
    assert net["detail"]["utilization"]["alternatives"]  # rx vs tx: ambiguous, both listed
    assert got["USE:node_filesystem"]["roles"]["utilization"] == "node_filesystem_avail_bytes"


async def test_unfilled_role_where_cites_a_siblings_code_registration(play):
    """telemetry-nerd-012: when catalog_context has already located a sibling metric of the
    same scope in the repo's code, the unfilled role's `where` points near it as a place to
    register the new metric too, not just the entity."""
    svc, _ = play
    svc.ws.catalog_context(
        "default",
        [
            {
                "path": "node_exporter.py",
                "text": "from prometheus_client import Counter\n"
                'C = Counter("node_cpu_seconds", "CPU seconds.")\n',
            }
        ],
    )
    got = by_id(svc.ws.binding_suggest("default", "USE", limit=100))
    cpu = got["USE:node_cpu"]
    where = cpu["unfilled"][0]["suggest_instrumentation"]["where"]
    assert "near code: node_exporter.py:2" in where and "is registered" in where


async def test_unfilled_role_where_never_calls_a_docs_mention_a_code_registration(play):
    """telemetry-nerd-012 review: a markdown docs table or dashboard citation describes the
    metric, it is not a place to add code; `where` must say so honestly, never phrase it as
    "registered" the way an actual code citation is."""
    svc, _ = play
    svc.ws.catalog_context(
        "default",
        [{"path": "METRICS.md", "text": "| node_cpu_seconds_total | counter | CPU seconds |"}],
    )
    got = by_id(svc.ws.binding_suggest("default", "USE", limit=100))
    cpu = got["USE:node_cpu"]
    where = cpu["unfilled"][0]["suggest_instrumentation"]["where"]
    assert "documented at docs: METRICS.md:1" in where and "not a code location" in where
    assert "registered" not in where


async def test_unfilled_role_where_prefers_a_code_citation_over_a_docs_one(play):
    """telemetry-nerd-012: when the same sibling metric has both a docs mention and a real code
    registration (e.g. a markdown table plus the actual prometheus_client call), the code one
    is the useful lead and wins, never the docs mention."""
    svc, _ = play
    svc.ws.catalog_context(
        "default",
        [
            {"path": "METRICS.md", "text": "| node_cpu_seconds_total | counter | CPU seconds |"},
            {
                "path": "node_exporter.py",
                "text": "from prometheus_client import Counter\n"
                'C = Counter("node_cpu_seconds", "CPU seconds.")\n',
            },
        ],
    )
    got = by_id(svc.ws.binding_suggest("default", "USE", limit=100))
    cpu = got["USE:node_cpu"]
    where = cpu["unfilled"][0]["suggest_instrumentation"]["where"]
    assert "near code: node_exporter.py:2" in where and "documented at" not in where


async def test_k8s_cpu_throttling_is_saturation(play):
    svc, _ = play
    k = by_id(svc.ws.binding_suggest("default", "USE", "k8s:container-cpu"))["USE:k8s_cpu"]
    assert k["roles"]["utilization"] == "container_cpu_usage_seconds_total"
    assert k["roles"]["saturation"] == "container_cpu_cfs_throttled_periods_total"
    sat = k["detail"]["saturation"]
    assert "container_cpu_cfs_periods_total" in sat["expr"]
    assert [a["metric"] for a in sat["alternatives"]] == [
        "container_cpu_cfs_throttled_seconds_total"
    ]
    mem = by_id(svc.ws.binding_suggest("default", "USE", "k8s:container-memory"))["USE:k8s_memory"]
    assert mem["roles"]["utilization"] == "container_memory_working_set_bytes"
    assert mem["roles"]["errors"] == "container_oom_events_total"


async def test_generic_prefix_scopes_and_percentiles(tmp_path):
    d = synthetic(
        ("shop_requests_total", "counter"),
        ("shop_request_errors_total", "counter"),
        ("shop_request_duration_seconds", "histogram"),
        ("shop_request_duration_quantiles", "summary"),
        ("shop_requests_in_flight", "gauge"),
        ("billing_requests_total", "counter"),
        ("billing_request_latency_seconds", "summary"),
        ("up", "gauge"),
    )
    svc, _ = await learned(tmp_path, d)
    got = by_id(svc.ws.binding_suggest("default", limit=100))
    shop = got["RED:app:shop"]
    assert shop["key"] == "shop" and shop["roles"] == {
        "rate": "shop_requests_total",
        "errors": "shop_request_errors_total",
        "duration": "shop_request_duration_seconds",
    }
    assert shop["detail"]["errors"]["alternatives"][0]["form"] == "label_split"
    assert got["littles_law:app:shop"]["roles"]["concurrency"] == "shop_requests_in_flight"
    # only a summary exists: offered, but flagged unusable for means and merging
    bill = got["RED:app:billing"]["detail"]["duration"]
    assert bill["form"] == "summary" and "not mergeable" in bill["note"]


async def test_generic_scope_leaves_known_names_to_their_scope(play):
    svc, _ = play
    ids = [s["id"] for s in svc.ws.binding_suggest("default", limit=100)["suggestions"]]
    assert not any(i.startswith(("RED:app", "littles_law:app")) for i in ids)


async def test_key_names_a_scope_or_an_entity(tmp_path):
    d = synthetic(
        ("shop_requests_total", "counter"),
        ("shop_request_duration_seconds", "histogram"),
        ("other_requests_total", "counter"),
        ("other_request_duration_seconds", "histogram"),
    )
    svc, _ = await learned(tmp_path, d)
    one = svc.ws.binding_suggest("default", "RED", "shop")["suggestions"]
    assert [s["id"] for s in one] == ["RED:app:shop"] and one[0]["key"] == "shop"
    both = svc.ws.binding_suggest("default", "RED", "app")["suggestions"]
    assert {s["key"] for s in both} == {"shop", "other"}
    none = svc.ws.binding_suggest("default", "USE")
    assert none["suggestions"] == []


async def test_otel_entity_key_relabels_the_binding(play):
    svc, _ = play
    (s,) = svc.ws.binding_suggest("default", "RED", "checkout", limit=100)["suggestions"][:1]
    assert s["key"] == "checkout"
    one = by_id(svc.ws.binding_suggest("default", "RED", "http.server"))
    assert one["RED:otel_http"]["key"] == "http.server"


async def test_part_of_relation_offers_the_error_counter(tmp_path):
    d = synthetic(
        ("api_requests_total", "counter"),
        ("api_request_duration_seconds", "histogram"),
        ("api_bad_responses_total", "counter"),
        ("api_5xx_total", "counter"),
    )
    svc, mcp = await learned(tmp_path, d)
    before = by_id(svc.ws.binding_suggest("default", "RED"))["RED:app:api"]
    assert before["detail"]["errors"]["form"] == "label_split"
    await call(
        mcp,
        "catalog_relate",
        {
            "source": "default",
            "claims": [
                {
                    "subject": "api_5xx_total",
                    "kind": "part_of",
                    "object": "api_requests_total",
                    "confidence": 0.8,
                    "basis": "5xx responses are requests",
                }
            ],
        },
    )
    after = by_id(svc.ws.binding_suggest("default", "RED"))["RED:app:api"]
    assert after["roles"]["errors"] == "api_5xx_total"
    assert after["detail"]["errors"]["basis"] == ["relation"]
    assert "relation" in after["detail"]["rate"]["basis"]


async def test_deterministic_and_order_independent(play):
    svc, _ = play
    entries = svc.ws.catalog_list("default")
    a = suggest_bindings(entries, limit=100)
    shuffled = list(entries)
    random.Random(7).shuffle(shuffled)
    assert suggest_bindings(shuffled, limit=100) == a
    assert [s["score"] for s in a["suggestions"]] == sorted(
        (s["score"] for s in a["suggestions"]), reverse=True
    )


async def test_unknown_kind_is_rejected(play):
    svc, _ = play
    with pytest.raises(ValueError, match="unknown binding kind"):
        svc.ws.binding_suggest("default", "SLO")


async def test_mcp_suggest_then_accept(play):
    _, mcp = play
    out = await call(
        mcp, "binding_suggest", {"source": "default", "kind": "littles_law", "key": "http.server"}
    )
    (s,) = out["suggestions"]
    assert s["id"] == "littles_law:otel_http" and "already_bound" not in s
    ok = await call(
        mcp,
        "binding_accept",
        {
            "source": "default",
            "id": s["id"],
            "basis": "checked names and types in catalog_get",
            "key": "http.server",
        },
    )
    assert ok["effective"] and ok["binding"]["roles"] == s["roles"] and ok["gaps"] == []
    rel = await call(mcp, "catalog_relations", {"source": "default"})
    assert [b["key"] for b in rel["bindings"]] == ["http.server"]
    again = await call(
        mcp, "binding_suggest", {"source": "default", "kind": "littles_law", "key": "http.server"}
    )
    assert again["suggestions"][0]["already_bound"]["origin"] == "claude"


async def test_accept_overrides_must_be_offered_alternatives(play):
    _, mcp = play
    bad = await call(
        mcp,
        "binding_accept",
        {
            "source": "default",
            "id": "RED:otel_http",
            "basis": "b",
            "overrides": {"duration": "node_load1"},
        },
    )
    assert "not a candidate" in bad
    ok = await call(
        mcp,
        "binding_accept",
        {
            "source": "default",
            "id": "RED:otel_http",
            "basis": "ms histogram is what the app emits",
            "overrides": {"duration": "http_server_duration_milliseconds", "errors": None},
        },
    )
    assert ok["binding"]["roles"]["duration"] == "http_server_duration_milliseconds"
    assert ok["binding"]["roles"]["errors"] is None and len(ok["gaps"]) == 1
    missing = await call(
        mcp, "binding_accept", {"source": "default", "id": "RED:nope", "basis": "b"}
    )
    assert "no suggestion" in missing


async def test_a_classic_histogram_listed_only_by_its_members_is_suggested_by_base(tmp_path):
    """6gp: Prometheus/VM list __name__ values only (X_bucket/_sum/_count, no X); learn
    catalogues X so the RED duration role names the histogram, not a member series."""
    names = ("shop_request_duration_seconds" + s for s in ("_bucket", "_sum", "_count"))
    d = Discovery(
        (MetricInfo("shop_requests_total", "counter"), *(MetricInfo(n) for n in names)),
        (), {"shop_request_duration_seconds": "classic"}, None, 1.0, (), False,
    )  # fmt: skip
    svc, _ = await learned(tmp_path, d)
    shop = by_id(svc.ws.binding_suggest("default", limit=100))["RED:app:shop"]
    assert shop["roles"]["duration"] == "shop_request_duration_seconds"


def ecs_discovery() -> Discovery:
    """An ECS-shaped Elasticsearch/OpenSearch discovery (sources/elasticsearch.py): field paths,
    not Prometheus names, and no TYPE/HELP metadata (field_caps only ever yields gauge/counter
    for a time_series_metric/metric_type field, never for a plain ECS field)."""
    return Discovery(
        tuple(
            MetricInfo(n)
            for n in (
                "event.duration",
                "http.response.status_code",
                "event.outcome",
                "service.name",
                "url.path",
            )
        ),
        (),
        {},
        None,
        1.0,
        ("cardinality_unavailable",),
        False,
        naming="fields",
    )


async def test_ecs_red_and_littles_law(tmp_path):
    svc, _ = await learned(tmp_path, ecs_discovery())
    got = by_id(svc.ws.binding_suggest("default", limit=100))
    red = got["RED:ecs_http"]
    assert red["key"] == "ecs.http" and red["join_on"] == ["service.name"]
    assert red["roles"] == {
        "rate": "event.duration",
        "errors": "event.duration",
        "duration": "event.duration",
    }
    d = red["detail"]
    assert d["rate"]["form"] == "es_field_rate" and "value_count" in d["rate"]["expr"]
    assert d["errors"]["form"] == "es_label_split" and "500" in d["errors"]["expr"]
    assert "event.outcome" in d["errors"]["note"]  # alternative filter, mentioned not offered
    assert d["duration"]["form"] == "es_stats" and "stats" in d["duration"]["expr"]
    assert "histogram_quantile" not in d["duration"]["expr"]  # not a PromQL histogram
    assert red["caveat"]
    # role_claim boost: the ecs pack claims role="latency" for event.duration
    assert d["duration"]["confidence"] > 0.85

    ll = got["littles_law:ecs_http"]
    assert ll["roles"]["arrival_rate"] == "event.duration"
    assert ll["roles"]["latency"] == "event.duration"
    assert ll["roles"]["concurrency"] is None
    (u,) = ll["unfilled"]
    assert u["role"] == "concurrency" and "ecs.http" in u["suggest_instrumentation"]["where"]


async def test_ecs_pack_claims_flow_into_the_catalog(tmp_path):
    svc, _ = await learned(tmp_path, ecs_discovery())
    entry = svc.ws.catalog_entry("default", "event.duration")
    assert entry.fields["unit"].value == "ns" and entry.fields["unit"].origin == "pack"
    assert entry.fields["type"].value == "histogram" and entry.fields["role"].value == "latency"
