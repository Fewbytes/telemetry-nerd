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
