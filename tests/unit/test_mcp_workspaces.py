import json

from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import make_service


async def call(mcp, name, args=None):
    async with Client(mcp) as client:
        r = await client.call_tool(name, args or {})
    block = r.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_create_makes_new_workspace_active_with_no_panels(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    out = json.loads(await call(mcp, "workspace_create", {"title": "p99", "question": "why?"}))
    assert out["workspace"]["title"] == "p99" and out["previous"] == "w1"
    assert out["url"] == "http://x" and len(json.dumps(out)) < 2048
    brief = json.loads(await call(mcp, "workspace_get"))
    assert brief["workspace"]["id"] == out["workspace"]["id"] != "w1"
    assert brief["panels"] == []


async def test_list_hides_archived_and_caps_at_20_with_more(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    for i in range(29):
        await call(mcp, "workspace_create", {"title": f"t{i}"})
    out = await call(mcp, "workspace_list")
    assert len(out) < 2048
    data = json.loads(out)
    assert len(data["workspaces"]) == 20 and data["more"] == 10
    first = data["workspaces"][-1]["id"]
    await call(mcp, "workspace_update", {"id": first, "archived": True})
    data = json.loads(await call(mcp, "workspace_list"))
    assert first not in [w["id"] for w in data["workspaces"]] and data["more"] == 9
    shown = json.loads(await call(mcp, "workspace_list", {"include_archived": True}))
    assert shown["more"] == 10


async def test_switch_unknown_id_is_a_tool_error_with_hint(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    async with Client(mcp) as c:
        r = await c.call_tool("workspace_switch", {"id": "w99"})
    assert r.is_error
    assert "hint" in r.content[0].text  # type: ignore[union-attr]


async def test_switch_and_update_results_are_compact_and_next_call_sees_new_workspace(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    await call(mcp, "workspace_create", {"title": "second"})
    out = await call(mcp, "workspace_switch", {"id": "w1"})
    assert len(out) < 2048 and json.loads(out)["url"] == "http://x"
    assert json.loads(await call(mcp, "workspace_get"))["workspace"]["id"] == "w1"
    upd = await call(mcp, "workspace_update", {"id": "w1", "title": "renamed"})
    assert json.loads(upd)["workspace"]["title"] == "renamed"
    async with Client(mcp) as c:
        r = await c.call_tool("workspace_update", {"id": "w1", "archived": True})
    assert r.is_error  # the active workspace cannot be archived


async def test_activity_default_window_is_the_active_workspaces_tail(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    for i in range(60):
        svc.ws.hypothesis_create(f"h{i} in w1", "user")
    await call(mcp, "workspace_create", {"title": "second"})
    for i in range(3):
        svc.ws.hypothesis_create(f"h{i} in w2", "user")
    out = json.loads(await call(mcp, "workspace_activity"))
    assert [e["type"] for e in out["events"]].count("hypothesis.created") == 3
    assert len(out["events"]) == 4  # + workspace.opened
    assert out["truncated"] is False


async def test_switch_result_stays_small_with_many_long_open_threads(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "workspace_create", {"title": "second"})
    await call(mcp, "workspace_switch", {"id": "w1"})
    for i in range(12):
        svc.ws.ask("x" * 300, "user", anchor=None)
    await call(mcp, "workspace_create", {"title": "third"})
    out = await call(mcp, "workspace_switch", {"id": "w1"})
    assert len(out) < 2048
    assert "more_open_threads" not in json.loads(out)
    await call(mcp, "workspace_update", {"id": "w1", "title": "t" * 1300})
    await call(mcp, "workspace_create", {"title": "fourth"})
    out = await call(mcp, "workspace_switch", {"id": "w1"})
    assert len(out) < 2048
    data = json.loads(out)
    assert data["more_open_threads"] > 0
    assert len(data["open_threads"]) + data["more_open_threads"] == 10  # WorkspaceOps caps at 10
