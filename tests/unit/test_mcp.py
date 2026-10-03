import json

import pytest
from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import FakeSource, NonFiniteSource, make_service


def text_of(result) -> str:
    """Narrow the content union to TextContent (our tools always return text)."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def call(mcp, name, args):
    async with Client(mcp) as client:
        return await client.call_tool(name, args)


async def test_query_then_show(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://127.0.0.1:7070")
    q = await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    assert not q.is_error
    out = json.loads(text_of(q))
    assert out["dataset"] == "d1"
    assert out["summary"]["series_count"] == 2

    s = await call(mcp, "show", {"dataset": "d1", "question": "Stable?"})
    assert not s.is_error
    shown = json.loads(text_of(s))
    assert shown["panel"] == "p1"
    assert shown["url"] == "http://127.0.0.1:7070/#/panel/p1"


async def test_show_without_question_is_tool_error(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    s = await call(mcp, "show", {"dataset": "d1", "question": ""})
    assert s.is_error
    assert "question" in text_of(s)


async def test_show_with_agent_learned_unit(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    # "up" has no inferrable suffix; Claude knows the unit from context and passes it
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    s = await call(mcp, "show", {"dataset": "d1", "question": "Up?", "unit": "boolean"})
    assert not s.is_error
    panel = svc.workspace.get_panel("p1")
    assert panel.spec["y"]["unit"] == "boolean"
    assert panel.spec["y"]["unit_provenance"] == "provided by claude"


async def test_rejected_chart_explains_rule(tmp_path):
    mcp = build_mcp(make_service(tmp_path, FakeSource(n_series=8)), "http://x")
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    s = await call(mcp, "show", {"dataset": "d1", "question": "Which is slow?"})
    assert s.is_error
    assert "series_budget" in text_of(s)


async def test_source_error_includes_hint(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    q = await call(mcp, "query", {"expr": "up", "source": "nope"})
    assert q.is_error
    assert "hint:" in text_of(q)


async def test_non_finite_values_yield_strict_json(tmp_path):
    mcp = build_mcp(make_service(tmp_path, NonFiniteSource()), "http://x")
    q = await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    assert not q.is_error
    out = json.loads(text_of(q), parse_constant=lambda c: pytest.fail(c))
    assert "non_finite" in out["summary"]["caveats"]


SCOPE = {
    "source": "default",
    "selector": "up",
    "start": "now-2h",
    "end": "now-1h",
    "step": "1m",
    "aggregation": "avg",
}


async def _finding_setup(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    return svc, mcp


async def test_annotate_accepts_relative_time(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    r = await call(mcp, "annotate", {"kind": "event", "at": "now-5m", "label": "deploy"})
    assert not r.is_error, text_of(r)
    a = json.loads(text_of(r))["annotation"]
    assert a["label"] == "deploy"
    assert a["t_start_ms"] is not None
    ev = svc.log.since(0)[-1]
    assert ev.actor == "claude" and ev.type == "annotation.created"


async def test_annotate_invalid_is_tool_error(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "annotate", {"kind": "event"})
    assert r.is_error
    assert "t_start_ms" in text_of(r)


async def test_hypothesis_create_and_update(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    h = json.loads(text_of(await call(mcp, "hypothesis_create", {"statement": "cache cold"})))
    assert h["hypothesis"] == "h1"
    u = await call(mcp, "hypothesis_update", {"hypothesis": "h1", "status": "refuted"})
    assert json.loads(text_of(u)) == {"hypothesis": "h1", "status": "refuted"}
    bad = await call(mcp, "hypothesis_update", {"hypothesis": "h99", "status": "refuted"})
    assert bad.is_error


async def test_finding_create_valid(tmp_path):
    _, mcp = await _finding_setup(tmp_path)
    r = await call(
        mcp,
        "finding_create",
        {
            "claim": "up is flat",
            "scope": SCOPE,
            "evidence": [
                {
                    "kind": "statistic",
                    "dataset": "d1",
                    "name": "mean",
                    "value": 1.0,
                    "method": "m",
                    "interval": [0.9, 1.1],
                }
            ],
        },
    )
    assert not r.is_error, text_of(r)
    out = json.loads(text_of(r))
    assert out["finding"] == "f1"
    assert out["url"] == "http://x/#/finding/f1"


async def test_finding_missing_scope_field_names_path(tmp_path):
    _, mcp = await _finding_setup(tmp_path)
    scope = {k: v for k, v in SCOPE.items() if k != "step"}
    r = await call(
        mcp,
        "finding_create",
        {"claim": "c", "scope": scope, "evidence": [{"kind": "panel", "panel": "p1"}]},
    )
    assert r.is_error
    assert "scope.step: field required" in text_of(r)


async def test_finding_statistic_without_interval_mentions_rule(tmp_path):
    _, mcp = await _finding_setup(tmp_path)
    r = await call(
        mcp,
        "finding_create",
        {
            "claim": "c",
            "scope": SCOPE,
            "evidence": [
                {"kind": "statistic", "dataset": "d1", "name": "mean", "value": 1.0, "method": "m"}
            ],
        },
    )
    assert r.is_error
    assert "no_uncertainty" in text_of(r)


async def test_gap_create(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(
        mcp,
        "gap_create",
        {
            "missing_signal": "queue depth",
            "needed_for": "saturation",
            "suggestion": {"name": "queue_depth", "type": "gauge"},
        },
    )
    assert not r.is_error, text_of(r)
    assert json.loads(text_of(r))["gap"] == "g1"


async def test_reply_and_workspace_get_open_threads(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    t = svc.ws.ask("why slow?", "user")
    before = json.loads(text_of(await call(mcp, "workspace_get", {})))
    assert [x["id"] for x in before["open_threads"]] == [t.id]
    r = await call(mcp, "reply", {"thread": t.id, "text": "checking"})
    assert not r.is_error, text_of(r)
    assert json.loads(text_of(r))["message"]
    after = json.loads(text_of(await call(mcp, "workspace_get", {})))
    assert after["open_threads"] == []


async def test_workspace_activity_and_reply_unknown_thread(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    svc.ws.ask("q", "user")
    a = json.loads(text_of(await call(mcp, "workspace_activity", {"since": 0})))
    assert a["events"]
    bad = await call(mcp, "reply", {"thread": "t99", "text": "x"})
    assert bad.is_error


async def test_highlight_and_unhighlight_tools(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    h = svc.ws.hypothesis_create("saturation", "claude")
    r = await call(mcp, "highlight", {"object": h.id, "note": "look", "seconds": 30})
    assert not r.is_error, text_of(r)
    e = [x for x in svc.ws.log.since(0) if x.type == "object.highlighted"][-1]
    assert e.payload == {"note": "look", "ttl_ms": 30_000} and e.actor == "claude"
    await call(mcp, "highlight", {"object": h.id, "seconds": 0})
    e = [x for x in svc.ws.log.since(0) if x.type == "object.highlighted"][-1]
    assert e.payload["ttl_ms"] is None
    default = await call(mcp, "highlight", {"object": h.id})
    assert not default.is_error
    e = [x for x in svc.ws.log.since(0) if x.type == "object.highlighted"][-1]
    assert e.payload["ttl_ms"] == 300_000
    assert not (await call(mcp, "unhighlight", {"object": h.id})).is_error
    assert (await call(mcp, "highlight", {"object": "p99"})).is_error
    assert (await call(mcp, "highlight", {"object": h.id, "seconds": -1})).is_error


# tests/unit/test_mcp.py
async def test_suggest_y_view(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://127.0.0.1:7070")
    ds = json.loads(
        text_of(await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"}))
    )["dataset"]
    pid = json.loads(text_of(await call(mcp, "show", {"dataset": ds, "question": "Up?"})))["panel"]
    out = json.loads(
        text_of(
            await call(
                mcp,
                "suggest_y_view",
                {
                    "panel": pid,
                    "mode": "zero",
                    "label": "from zero",
                    "reason": "show the dip in proportion",
                },
            )
        )
    )
    assert out == {"panel": pid, "view": "v1", "warnings": []}
    bad = await call(
        mcp, "suggest_y_view", {"panel": pid, "mode": "zero", "label": "z", "reason": ""}
    )
    assert bad.is_error and "reason" in text_of(bad)
    log = await call(
        mcp,
        "suggest_y_view",
        {"panel": pid, "mode": "log", "label": "log", "reason": "r", "lo": 1, "hi": 2},
    )
    assert log.is_error and "band" in text_of(log)


async def test_show_marginal(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://127.0.0.1:7070")
    ds = json.loads(
        text_of(await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"}))
    )["dataset"]
    pid = json.loads(text_of(await call(mcp, "show", {"dataset": ds, "question": "Up?"})))["panel"]
    out = json.loads(
        text_of(
            await call(
                mcp,
                "show_marginal",
                {"panel": pid, "reference": "previous", "reason": "did the level shift?"},
            )
        )
    )
    assert out["basis"] == "samples" and out["n"]["now"] > 0 and "not requests" in out["what"]
    bad = await call(mcp, "show_marginal", {"panel": pid, "reference": "yesterday", "reason": "r"})
    assert bad.is_error and "unknown reference" in text_of(bad)


async def test_finding_with_unknown_uncertainty_is_recorded_and_flagged(tmp_path):
    """Spec §5.3 (x2x.1): unknown uncertainty is citable; the result and workspace_get say so."""
    _, mcp = await _finding_setup(tmp_path)
    r = await call(
        mcp,
        "finding_create",
        {
            "claim": "up is about 1",
            "scope": SCOPE,
            "evidence": [
                {"kind": "statistic", "dataset": "d1", "name": "mean", "value": 1.0,
                 "method": "read off the chart", "uncertainty_unknown": True}
            ],
        },
    )  # fmt: skip
    assert not r.is_error, text_of(r)
    out = json.loads(text_of(r))
    assert [(u["evidence"], u["flag"]) for u in out["uncertainty"]] == [(0, "uncertainty_unknown")]
    assert "uncertainty unknown" in out["uncertainty"][0]["message"]
    brief = json.loads(text_of(await call(mcp, "workspace_get", {})))
    assert brief["findings"][0]["uncertainty"] == ["uncertainty_unknown"]


@pytest.mark.parametrize(
    "args",
    [
        {"missing_signal": "q", "needed_for": "sat", "suggestion": "queue_depth gauge"},
        {"missing_signal": "q", "needed_for": "sat", "suggestion": "queue_depth (gauge)"},
        {"description": "queue depth, needed for saturation", "suggestion": "queue_depth:gauge"},
        {"missing_signal": "q", "needed_for": "sat", "suggestion": {"metric": "queue_depth"}},
        {
            "missing_signal": "q",
            "needed_for": "sat",
            "suggestion": {"name": "queue_depth", "type": "Gauge", "labels": "a, b"},
        },
    ],
)
async def test_gap_create_forgiving_shapes(tmp_path, args):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "gap_create", args)
    assert not r.is_error, text_of(r)
    assert json.loads(text_of(r))["gap"] == "g1"


async def test_gap_create_error_shows_expected_json(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "gap_create", {"missing_signal": "q"})
    assert r.is_error
    msg = text_of(r)
    assert "needed_for" in msg and '"suggestion": {"name"' in msg
    r = await call(
        mcp,
        "gap_create",
        {"missing_signal": "q", "needed_for": "x", "suggestion": {"name": "m", "type": "weird"}},
    )
    assert r.is_error and "Expected" in text_of(r)
