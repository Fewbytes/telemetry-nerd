import json
import re
from pathlib import Path

import pytest
from mcp import Client

from telemetry_nerd.catalog.search import family_prefix, overview, reviewed, search
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import FakeSource, make_service

ROOT = Path(__file__).resolve().parent.parent.parent


def disc():
    infos = [
        MetricInfo("node_cpu_seconds_total", "counter", "CPU time", None),
        MetricInfo("node_memory_MemTotal_bytes", "gauge", "total memory", None),
        MetricInfo("node_boot_time_seconds", "gauge", "boot time", None),
        MetricInfo("app_requests_total", "counter", "requests", None),
        MetricInfo("app_queue_seconds", "gauge", "queue wait", None),
        MetricInfo("up", None, None, None),
    ]
    return Discovery(tuple(infos), (), {}, None, 1.0, (), False)


@pytest.fixture
def svc(tmp_path):
    return make_service(tmp_path, FakeSource(name="default", discovery=disc()))


async def call(mcp, name, args=None):
    async with Client(mcp) as client:
        res = await client.call_tool(name, args or {})
    return res, json.loads(res.content[0].text) if not res.is_error else res.content[0].text


async def test_learn_search_write_get_end_to_end(svc):
    mcp = build_mcp(svc, "http://x")
    _, out = await call(mcp, "source_learn", {"source": "default"})
    assert out["metrics"] == 6 and {f["family"] for f in out["families"]} >= {
        "node_cpu",
        "app_requests",
    }

    # use one metric so it becomes hot
    await svc.query("rate(app_requests_total[5m])", start="now-2h", end="now-1h")
    _, found = await call(mcp, "catalog_search", {"source": "default", "needs_review": True})
    rows = {r["metric"]: r for r in found["results"]}
    assert (
        found["results"][0]["metric"] == "app_requests_total" and rows["app_requests_total"]["hot"]
    )
    assert "node_cpu_seconds_total" not in rows  # the pack already interpreted it

    _, wrote = await call(
        mcp,
        "catalog_write",
        {
            "source": "default",
            "claims": [
                {
                    "metric": "app_queue_seconds",
                    "field": "unit",
                    "value": "s",
                    "confidence": 0.8,
                    "basis": "HELP says queue wait time in seconds",
                },
                {
                    "metric": "app_queue_seconds",
                    "field": "role",
                    "value": "latency",
                    "confidence": 0.7,
                    "basis": "name and HELP describe waiting",
                },
            ],
        },
    )
    assert [r["status"] for r in wrote["results"]] == ["accepted", "accepted"]
    assert all(r["effective"] for r in wrote["results"])

    _, got = await call(mcp, "catalog_get", {"source": "default", "metric": "app_queue_seconds"})
    assert got["resolved"]["role"] == "latency"
    origins = {c["origin"] for c in got["claims"]["role"]}
    assert origins == {"claude"}
    assert any(
        c["citation"].startswith("HELP says")
        for c in got["claims"]["unit"]
        if c["origin"] == "claude"
    )

    _, again = await call(
        mcp, "catalog_search", {"source": "default", "prefix": "app_queue", "needs_review": True}
    )
    assert again["total"] == 0  # reviewed now


async def test_claude_never_overrides_the_user(svc):
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "source_learn")
    svc.ws.catalog_claim("default", "app_queue_seconds", "unit", "ms", "user", "user")
    _, wrote = await call(
        mcp,
        "catalog_write",
        {
            "source": "default",
            "claims": [
                {
                    "metric": "app_queue_seconds",
                    "field": "unit",
                    "value": "s",
                    "confidence": 0.9,
                    "basis": "docs",
                }
            ],
        },
    )
    (r,) = wrote["results"]
    assert (r["status"], r["effective"], r["outranked_by"]) == ("accepted", False, "user")
    assert svc.ws.catalog_entry("default", "app_queue_seconds").fields["unit"].value == "ms"
    assert svc.ws.catalog_facts("default", "app_queue_seconds").unit == "ms"


async def test_claude_outranks_pack_metadata_and_rules(svc):
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "source_learn")
    _, wrote = await call(
        mcp,
        "catalog_write",
        {
            "source": "default",
            "claims": [
                {
                    "metric": "node_cpu_seconds_total",
                    "field": "unit",
                    "value": "ms",
                    "confidence": 0.8,
                    "basis": "exporter patched to emit ms",
                }
            ],
        },
    )
    assert wrote["results"][0]["effective"] is True
    e = svc.ws.catalog_entry("default", "node_cpu_seconds_total")
    assert e.fields["unit"].origin == "claude" and "unit" in e.conflicts()


@pytest.mark.parametrize(
    ("claim", "reason"),
    [
        (
            {"metric": "up", "field": "role", "value": "state", "confidence": 1.0, "basis": "x"},
            "confidence",
        ),
        (
            {"metric": "up", "field": "role", "value": "state", "confidence": 0.0, "basis": "x"},
            "confidence",
        ),
        ({"metric": "up", "field": "role", "value": "state", "confidence": 0.8}, "basis"),
        (
            {"metric": "up", "field": "role", "value": "state", "confidence": 0.8, "basis": "  "},
            "basis",
        ),
        (
            {"metric": "nope", "field": "role", "value": "state", "confidence": 0.8, "basis": "x"},
            "source_learn",
        ),
        (
            {"metric": "up", "field": "bogus", "value": "x", "confidence": 0.8, "basis": "x"},
            "unknown catalog field",
        ),
        (
            {"metric": "up", "field": "unit", "value": " ", "confidence": 0.8, "basis": "x"},
            "non-empty",
        ),
        (
            {"metric": "up", "field": "type", "value": "untyped", "confidence": 0.8, "basis": "x"},
            "invalid type",
        ),
    ],
)
async def test_bad_claims_are_rejected_individually(svc, claim, reason):
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "source_learn")
    good = {
        "metric": "up",
        "field": "role",
        "value": "state",
        "confidence": 0.8,
        "basis": "up is 1 when the target is scraped",
    }
    _, out = await call(mcp, "catalog_write", {"source": "default", "claims": [claim, good]})
    bad, ok = out["results"]
    assert bad["status"] == "rejected" and reason in bad["reason"]
    assert ok["status"] == "accepted"  # one bad claim does not abort the batch
    assert svc.ws.catalog_entry("default", "up").fields["role"].origin == "claude"


async def test_batch_limit_and_unknown_source(svc):
    mcp = build_mcp(svc, "http://x")
    res, text = await call(mcp, "catalog_write", {"source": "default", "claims": [{}] * 201})
    assert res.is_error and "at most 200" in text
    res, _ = await call(mcp, "source_learn", {"source": "nope"})
    assert res.is_error
    res, _ = await call(mcp, "catalog_get", {"source": "default", "metric": "never_learned"})
    assert res.is_error


async def test_claude_writes_are_internal_events_and_never_use_other_origins(svc):
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "source_learn")
    await call(
        mcp,
        "catalog_write",
        {
            "source": "default",
            "claims": [
                {
                    "metric": "up",
                    "field": "role",
                    "value": "state",
                    "confidence": 0.8,
                    "basis": "b",
                    "origin": "user",
                }
            ],
        },
    )
    ev = [e for e in svc.ws.log.since(0) if e.type == "catalog.claimed"][-1]
    assert (ev.actor, ev.klass, ev.payload["origin"]) == ("claude", "internal", "claude")


def test_family_prefix_and_review_helpers(svc):
    assert family_prefix("node_cpu_seconds_total") == "node_cpu" and family_prefix("up") == "up"
    assert family_prefix("a_b") == "a_b"


async def test_hot_metrics_only_count_catalogued_names_in_this_sources_datasets(svc):
    await svc.learn("default")
    await svc.query("sum(rate(app_requests_total[5m])) by (job)", start="now-2h", end="now-1h")
    assert svc.ws.catalog_hot("default") == {"app_requests_total"}
    assert svc.ws.catalog_hot("other") == set()


async def test_search_filters_and_overview(svc):
    await svc.learn("default")
    entries = svc.ws.catalog.list_entries("default")
    assert {r["metric"] for r in search(entries, set(), query="BOOT")["results"]} == {
        "node_boot_time_seconds"
    }
    assert search(entries, set(), prefix="node_")["total"] == 3
    capped = search(entries, set(), limit=2)
    assert (capped["total"], capped["returned"]) == (6, 2)
    ov = overview(entries)
    assert {o["family"] for o in ov} >= {"node_cpu", "node_memory", "app_requests", "up"}
    assert all(o["reviewed"] <= o["metrics"] for o in ov)
    assert not any(reviewed(e) for e in entries if e.metric == "app_queue_seconds")


async def test_needs_review_filtering_a_real_match_to_empty_never_suggests_instrumenting(svc):
    """telemetry-nerd-012: `app_queue_seconds` is a real, catalogued queue metric. Once it is
    reviewed, `needs_review=True` filters it out of the results (by design), but that is a
    filter hiding a real match, not "nothing in the catalog answers this" - the instrumentation
    hint must not fire and claim a queue metric needs adding when one already exists."""
    await svc.learn("default")
    svc.ws.catalog_claim("default", "app_queue_seconds", "role", "saturation", "claude", "claude",
                          confidence=0.5, verified_by="basis")  # fmt: skip
    entries = svc.ws.catalog.list_entries("default")
    assert any(e.metric == "app_queue_seconds" and reviewed(e) for e in entries)
    out = search(entries, set(), query="queue", needs_review=True)
    assert out["total"] == 0
    assert "suggest_instrumentation" not in out


# plugin text ---------------------------------------------------------------------------------
async def registered_tools(svc):
    async with Client(build_mcp(svc, "http://x")) as c:
        return {t.name for t in (await c.list_tools()).tools}


@pytest.mark.parametrize("path", ["skills/metric-learning/SKILL.md", "commands/learn.md"])
async def test_plugin_text_only_names_real_tools(svc, path):
    text = (ROOT / path).read_text()
    tools = await registered_tools(svc)
    mentioned = set(re.findall(r"`((?:source_learn|catalog_[a-z]+))", text))
    assert mentioned and mentioned <= tools, mentioned - tools


def test_skill_frontmatter_and_discipline_rules():
    text = (ROOT / "skills/metric-learning/SKILL.md").read_text()
    head = text.split("---")[1]
    assert "name: metric-learning" in head and "description:" in head
    for phrase in ("at most 0.9", "basis", "user > **claude", "timestamp"):
        assert phrase in text, phrase
    cmd = (ROOT / "commands/learn.md").read_text()
    assert cmd.startswith("---") and "description:" in cmd.split("---")[1]


async def test_source_defaults_to_default(svc):
    """catalog_* tools work with no `source` (Claude only ever has one; the eval failed 2x)."""
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "source_learn")
    res, found = await call(mcp, "catalog_search", {"query": "app_"})
    assert not res.is_error and "app_requests_total" in json.dumps(found)
    res, got = await call(mcp, "catalog_get", {"metric": "app_requests_total"})
    assert not res.is_error and got["metric"] == "app_requests_total"
    res, _ = await call(mcp, "catalog_relations")
    assert not res.is_error
    res, _ = await call(mcp, "binding_suggest")
    assert not res.is_error
    res, _ = await call(mcp, "catalog_write", {"claims": []})
    assert not res.is_error
    res, _ = await call(mcp, "catalog_relate", {"claims": []})
    assert not res.is_error
