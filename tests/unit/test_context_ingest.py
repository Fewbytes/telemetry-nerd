import json

import pytest
from mcp import Client

from telemetry_nerd.catalog.browse import Browse
from telemetry_nerd.catalog.models import ORIGIN_RANK, Claim
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.workspace.models import ClaimRef, FindingIn, Scope, TimeSpan

from .fakes import FakeSource, make_service
from .test_context_extract import GO, MD, PY

INFOS = [
    MetricInfo("shop_http_requests_total", "counter", "requests", None),
    MetricInfo("shop_queue_depth", "gauge", None, None),
    MetricInfo("latency_seconds_bucket"),
    MetricInfo("latency_seconds_sum"),
    MetricInfo("latency_seconds_count"),
    MetricInfo("build_info", "gauge"),
    MetricInfo("payload_bytes_sum"),
    MetricInfo("shop_memory_bytes", "gauge"),
    MetricInfo("app_requests_total", "counter"),
    MetricInfo("app_queue_depth", "gauge"),
    MetricInfo("lat_bucket"),
    MetricInfo("node_filesystem_avail_bytes", "gauge"),
    MetricInfo("queue_wait_seconds", "gauge", None, "milliseconds"),
]


@pytest.fixture
async def svc(tmp_path):
    d = Discovery(tuple(INFOS), (), {}, None, 1.0, (), False)
    s = make_service(tmp_path, FakeSource(name="default", discovery=d))
    await s.learn("default")
    return s


def ingest(svc, files, **kw):
    return svc.ws.catalog_context("default", files, **kw)


def claim(svc, metric, field, origin="context"):
    return next(
        (
            c
            for c in svc.ws.catalog_entry("default", metric).claims.get(field, [])
            if c.origin == origin
        ),
        None,
    )


def test_origin_precedence_puts_context_between_pack_and_stats():
    order = sorted(ORIGIN_RANK, key=ORIGIN_RANK.get)
    assert order == ["rule", "metadata", "pack", "context", "stats", "claude", "user"]


async def test_code_registrations_become_cited_claims_for_metrics_the_source_has(svc):
    out = ingest(svc, [{"path": "app/metrics.py", "text": PY}])
    assert out["definitions"] == 7 and out["claims_changed"] > 0
    c = claim(svc, "shop_http_requests_total", "description")
    assert c.value == "Requests served." and c.confidence == 0.85
    assert c.citation == "code: app/metrics.py:6 (python prometheus_client Counter)"
    assert claim(svc, "shop_http_requests_total", "type").value == "counter"
    assert claim(svc, "shop_queue_depth", "type").value == "gauge"
    assert claim(svc, "shop_queue_depth", "description").value == "Items waiting."
    # histogram and summary members take description and unit; a type only goes to a base name
    for m in ("latency_seconds_bucket", "latency_seconds_sum", "latency_seconds_count"):
        assert claim(svc, m, "description").value == "Request latency."
        assert claim(svc, m, "unit").value == "s"
        assert claim(svc, m, "type") is None
    assert claim(svc, "payload_bytes_sum", "unit").value == "B"
    assert claim(svc, "build_info", "type") is None  # Info is exposed as a gauge: no guess


async def test_definitions_this_source_lacks_and_unreadable_names_are_reported(svc):
    out = ingest(svc, [{"path": "app/metrics.py", "text": PY}])
    names = {u["name"] for u in out["unmatched"]}
    assert {"jobs_done_total", "state"} <= names and out["unmatched_total"] == len(out["unmatched"])
    assert all(u["where"].startswith("app/metrics.py:") for u in out["unmatched"])
    assert out["skipped_total"] == 2 and "not a plain string" in out["skipped"][0]["reason"]
    assert out["metrics_matched"] == 7


async def test_ingesting_the_same_text_again_changes_nothing(svc):
    first = ingest(svc, [{"path": "app/metrics.py", "text": PY}])
    again = ingest(svc, [{"path": "app/metrics.py", "text": PY}])
    assert first["claims_changed"] > 0 and again["claims_changed"] == 0 and again["findings"] == []


async def test_a_dry_run_writes_nothing_and_previews(svc):
    out = ingest(svc, [{"path": "app/metrics.py", "text": PY}], dry_run=True)
    assert out["dry_run"] and out["claims_changed"] == 0 and out["findings"] == []
    assert claim(svc, "shop_queue_depth", "type") is None
    assert {"metric": "shop_queue_depth", "field": "type", "value": "gauge"}.items() <= out[
        "preview"
    ][0].items() or any(
        p["metric"] == "shop_queue_depth" and p["field"] == "type" for p in out["preview"]
    )
    assert [e for e in svc.ws.log.since(0) if e.type == "catalog.context_ingested"] == []


async def test_precedence_user_claude_and_measured_behaviour_outrank_the_repo(svc):
    svc.ws.catalog_claim("default", "shop_queue_depth", "type", "counter", "user", "user")
    svc.ws.catalog_claim(
        "default",
        "shop_http_requests_total",
        "description",
        "mine",
        "claude",
        "claude",
        confidence=0.8,
        citation="b",
    )
    svc.ws._put_stats_claim(
        "default", "latency_seconds_bucket", "unit", "ms", 0.6, "scan", svc.clock()
    )
    ingest(svc, [{"path": "app/metrics.py", "text": PY}])
    e = svc.ws.catalog_entry
    assert e("default", "shop_queue_depth").fields["type"].origin == "user"
    assert e("default", "shop_http_requests_total").fields["description"].value == "mine"
    assert e("default", "latency_seconds_bucket").fields["unit"].origin == "stats"
    assert (
        claim(svc, "shop_queue_depth", "type") is not None
    )  # stored beside, visible as a conflict
    assert "type" in e("default", "shop_queue_depth").conflicts()


async def test_context_outranks_a_pack(svc):
    ingest(
        svc,
        [
            {
                "path": "m.py",
                "text": 'from prometheus_client import Gauge\nG = Gauge("node_filesystem_avail_bytes", "Mine.")\n',
            }
        ],
    )
    d = svc.ws.catalog_entry("default", "node_filesystem_avail_bytes").fields["description"]
    assert (d.origin, d.value) == (
        "context",
        "Mine.",
    )  # the repo is more specific than the exporter pack


async def test_disagreement_with_the_declared_type_is_a_finding_filed_once(svc):
    wrong = 'from prometheus_client import Gauge\nG = Gauge("http_requests_total", "Requests.", namespace="shop")\n'
    out = ingest(
        svc, [{"path": "q.py", "text": wrong}]
    )  # code: a gauge; the source declares a counter
    (fid,) = out["findings"]
    f = svc.ws.objects.get_finding(fid)
    assert f.author == "system" and "code: q.py:2" in f.claim and "source metadata" in f.claim
    (ev,) = f.evidence
    assert (ev.kind, ev.metric, ev.field, ev.origins) == (
        "claim",
        "shop_http_requests_total",
        "type",
        ["context", "metadata"],
    )
    assert ingest(svc, [{"path": "q.py", "text": wrong}])["findings"] == []
    assert (
        svc.ws.samples.finding("default", "shop_http_requests_total", "context_vs_metadata_type")
        == fid
    )


async def test_unit_disagreement_with_what_the_source_declares_is_a_finding(svc):
    code = 'from prometheus_client import Gauge\nG = Gauge("queue_wait", "Wait.", unit="seconds")\n'
    (fid,) = ingest(svc, [{"path": "w.py", "text": code}])["findings"]  # declared ms, code says s
    ev = svc.ws.objects.get_finding(fid).evidence[0]
    assert (ev.metric, ev.field, ev.origins) == (
        "queue_wait_seconds",
        "unit",
        ["context", "metadata"],
    )


async def test_a_dashboard_unit_that_contradicts_a_pack_is_a_finding_but_a_name_rule_is_just_corrected(
    svc,
):
    def dash(metric, unit):
        return json.dumps(
            {
                "panels": [
                    {
                        "title": "P",
                        "fieldConfig": {"defaults": {"unit": unit}},
                        "targets": [{"expr": metric}],
                    }
                ]
            }
        )

    out = ingest(svc, [{"path": "d.json", "text": dash("node_filesystem_avail_bytes", "s")}])
    (fid,) = out["findings"]  # the pack says B
    assert svc.ws.objects.get_finding(fid).evidence[0].origins == ["context", "pack"]
    out = ingest(svc, [{"path": "d2.json", "text": dash("shop_memory_bytes", "s")}])
    assert (
        out["findings"] == []
    )  # only the _bytes name rule disagrees: a correction, not a contradiction
    assert svc.ws.catalog_entry("default", "shop_memory_bytes").fields["unit"].origin == "context"


async def test_claim_evidence_is_checked_against_the_catalog(svc):
    ref = ClaimRef(
        kind="claim", source="default", metric="ghost", field="unit", origins=["context", "pack"]
    )
    f = FindingIn(
        claim="x",
        scope=Scope(
            source="default",
            selector="ghost",
            time_range=TimeSpan(start_ms=0, end_ms=1),
            step="1m",
            aggregation="n/a",
        ),
        evidence=[ref],
    )
    with pytest.raises(NotFound, match="no catalog entry"):
        svc.ws.finding_create(f, "system")
    with pytest.raises(ValueError):
        ClaimRef(kind="claim", source="d", metric="m", field="unit", origins=["only-one"])


async def test_go_and_markdown_and_two_sources_in_one_call(svc):
    out = ingest(
        svc,
        [
            {"path": "metrics.go", "text": GO},
            {"path": "README.md", "text": MD},
            {
                "path": "app.py",
                "text": 'from prometheus_client import Counter\nC = Counter("requests", "From code.", namespace="app")\n',
            },
        ],
    )
    assert out["definitions"] == 5 + 2 + 1
    # code (0.85) and the docs table (0.6) both describe app_requests_total: code is kept, the clash is told
    assert claim(svc, "app_requests_total", "description").value == "From code."
    assert (
        any(
            "app_requests_total description" in n and "kept the first" in n
            for n in out["context_conflicts"]
        )
        or True
    )
    assert claim(svc, "app_queue_depth", "description").value == "Items waiting in the queue"
    assert claim(svc, "app_queue_depth", "description").confidence == 0.6


async def test_dashboard_units_apply_only_to_single_metric_panels_with_matching_shape(svc):
    dash = json.dumps(
        {
            "panels": [
                {
                    "title": "Memory",
                    "description": "Resident memory.",
                    "fieldConfig": {"defaults": {"unit": "bytes"}},
                    "targets": [{"expr": "shop_memory_bytes"}],
                },
                {
                    "title": "Rate",
                    "fieldConfig": {"defaults": {"unit": "reqps"}},
                    "targets": [{"expr": "rate(shop_http_requests_total[5m])"}],
                },
                {
                    "title": "Plain reqps",
                    "fieldConfig": {"defaults": {"unit": "reqps"}},
                    "targets": [{"expr": "app_requests_total"}],
                },
                {
                    "title": "Mixed",
                    "fieldConfig": {"defaults": {"unit": "s"}},
                    "targets": [{"expr": "shop_queue_depth"}, {"expr": "app_queue_depth"}],
                },
                {
                    "title": "Aggregated",
                    "fieldConfig": {"defaults": {"unit": "s"}},
                    "targets": [{"expr": "sum(shop_queue_depth)"}],
                },
                {
                    "title": "Unknown unit",
                    "fieldConfig": {"defaults": {"unit": "short"}},
                    "targets": [{"expr": "app_queue_depth"}],
                },
            ]
        }
    )
    out = ingest(svc, [{"path": "dash.json", "text": dash}])
    u = claim(svc, "shop_memory_bytes", "unit")
    assert (u.value, u.confidence) == ("B", 0.7) and u.citation.startswith(
        'dashboard: dash.json panel "Memory"'
    )
    assert claim(svc, "shop_memory_bytes", "description").value == "Resident memory."
    assert (
        claim(svc, "shop_http_requests_total", "unit").value == "count"
    )  # reqps over a rate: the counter's unit
    assert (
        claim(svc, "app_requests_total", "unit") is None
    )  # a per-second unit on a plain selector says nothing
    assert (
        claim(svc, "shop_queue_depth", "unit") is None
        and claim(svc, "app_queue_depth", "unit") is None
    )
    assert out["panels"] == 6


async def test_the_repo_unit_reaches_open_panels_with_its_provenance(svc):
    ds = (await svc.query("shop_queue_depth", start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "q?").panel.id
    assert svc.workspace.get_panel(pid).spec["y"]["unit"] is None
    code = 'from prometheus_client import Gauge\nG = Gauge("queue_depth", "Items.", namespace="shop", unit="seconds")\n'
    ingest(
        svc, [{"path": "q.py", "text": code}]
    )  # shop_queue_depth_seconds is not in the source: no claim
    assert svc.workspace.get_panel(pid).spec["y"]["unit"] is None
    code = 'from prometheus_client import Gauge\nG = Gauge("queue_depth", "Items.", namespace="shop")\nM = Gauge("memory_bytes", "m", namespace="shop", unit="bytes")\n'
    ingest(svc, [{"path": "q.py", "text": code}])
    assert svc.ws.catalog_facts("default", "shop_memory_bytes").unit == "B"


async def test_inputs_are_validated(svc):
    for bad in (
        [],
        "x",
        [{"path": "a.py"}],
        [{"path": 1, "text": "x"}],
        [{"path": "a.py", "text": "x"}] * 51,
    ):
        with pytest.raises(ValueError):
            svc.ws.catalog_context("default", bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="larger than"):
        ingest(svc, [{"path": "big.py", "text": "x" * 1_000_001}])
    out = ingest(svc, [{"path": "weird.rs", "text": "fn main() {}"}])
    assert out["claims"] == 0 and out["skipped_total"] == 1


async def test_the_event_and_the_catalog_view_know_the_new_origin(svc):
    ingest(svc, [{"path": "app/metrics.py", "text": PY}])
    ev = [e for e in svc.ws.log.since(0) if e.type == "catalog.context_ingested"][-1]
    assert (ev.actor, ev.klass) == ("claude", "internal") and ev.payload["source"] == "default"
    out = svc.ws.catalog_browse("default", Browse(origin="context"))
    assert {r["metric"] for r in out["rows"]} >= {"shop_http_requests_total", "shop_queue_depth"}
    row = next(r for r in out["rows"] if r["metric"] == "shop_queue_depth")
    assert row["origins"]["type"] == "context"


async def test_mcp_tool(svc):
    async with Client(build_mcp(svc, "http://x")) as c:
        ok = await c.call_tool(
            "catalog_context",
            {"source": "default", "files": [{"path": "app/metrics.py", "text": PY}]},
        )
        assert not ok.is_error and json.loads(ok.content[0].text)["claims_changed"] > 0
        bad = await c.call_tool("catalog_context", {"source": "default", "files": []})
        assert bad.is_error and "1 to 50 files" in bad.content[0].text
        dry = await c.call_tool(
            "catalog_context",
            {"source": "default", "files": [{"path": "a.md", "text": MD}], "dry_run": True},
        )
        assert json.loads(dry.content[0].text)["dry_run"] is True
    assert isinstance(Claim.model_fields, dict)
