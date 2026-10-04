"""Retrospective over its surfaces (spec 2026-10-04): MCP tools, HTTP routes, the channel text
and the SessionStart hook lines."""

from __future__ import annotations

import json

import pytest
from mcp import Client
from mcp.types import TextContent
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.mcp.server import INSTRUCTIONS, build_mcp
from telemetry_nerd.retro.models import LessonScope
from tests.unit.fakes import NOW, make_service
from tests.unit.test_retro_ops import finding

HOSTS = ["testserver"]


@pytest.fixture
def svc(tmp_path):
    s = make_service(tmp_path)
    s.ws.catalog.relearn("default", ["checkout_latency"], NOW)
    return s


async def call(mcp, name, args=None, error=False):
    async with Client(mcp) as client:
        r = await client.call_tool(name, args or {})
    block = r.content[0]
    assert isinstance(block, TextContent)
    assert r.is_error is error, block.text
    return block.text if error else json.loads(block.text)


async def test_mcp_propose_surface_and_refute(svc):
    mcp = build_mcp(svc, "http://x")
    f1 = finding(svc, 'checkout_latency{service_name="checkout"}')
    text = await call(
        mcp, "lesson_propose",
        {"text": "split by region", "scope": {"source": "default"}, "evidence": [f1]},
        error=True,
    )  # fmt: skip
    assert "lesson_beyond_evidence" in text and "hint" in text
    out = await call(
        mcp, "lesson_propose",
        {"text": "split by region", "scope": {"source": "default", "service": "checkout"},
         "evidence": [f1], "expires": "30d"},
    )  # fmt: skip
    assert out["status"] == "proposed" and out["scope_check"]["covered_by"] == [f1]
    lesson = out["lesson"]
    assert lesson.startswith("ls")
    assert (await call(mcp, "lessons_for", {"source": "default", "services": ["checkout"]}))[
        "lessons"
    ] == []  # proposed, not approved
    res = await call(
        mcp, "catalog_propose",
        {"claims": [{"metric": "checkout_latency", "field": "unit", "value": "s",
                     "confidence": 0.8, "basis": "bucket bounds", "evidence": [f1]}]},
    )  # fmt: skip
    assert res["results"][0]["status"] == "proposed"
    listing = await call(mcp, "proposals_list")
    assert listing["pending"] == 2 and listing["lessons"][0]["scope"].endswith("service checkout")

    svc.retro.decide_lesson(lesson, "approve", "user")
    hit = await call(mcp, "lessons_for", {"source": "default", "services": ["checkout"]})
    assert [x["id"] for x in hit["lessons"]] == [lesson]
    assert (await call(mcp, "lessons_for", {"source": "default"}))["held"] == 1

    against = finding(svc, 'checkout_latency{service_name="checkout"}')
    out = await call(
        mcp, "lesson_refute", {"lesson": lesson, "evidence": [against], "reason": "no longer"}
    )
    assert out == {"lesson": lesson, "status": "refuted"}
    await call(mcp, "lessons_for", {"source": "default", "service": "x"}, error=True)


def test_instructions_mention_lessons_and_wrap():
    text = " ".join(INSTRUCTIONS.split())
    for word in ("lessons_for", "catalog_propose", "lesson_propose", "/telemetry-nerd:wrap"):
        assert word in text


def test_http_routes(svc):
    f1 = finding(svc, "checkout_latency")
    lesson = svc.retro.lesson_propose(
        "always check the count", LessonScope(source="default"), [f1],
    )  # fmt: skip
    (r,) = svc.retro.catalog_propose(
        "default",
        [{"metric": "checkout_latency", "field": "unit", "value": "s", "confidence": 0.5,
          "basis": "b"}],
    )  # fmt: skip
    with TestClient(create_app(svc, allowed_hosts=HOSTS)) as c:
        data = c.get("/api/proposals").json()
        assert data["pending"] == 2
        assert c.get("/api/proposals/summary").json() == {"lessons": {}, "pending": 2}
        bad = c.post(f"/api/proposals/{lesson.id}/decide", json={"decision": "maybe"})
        assert bad.status_code == 400
        ok = c.post(
            f"/api/proposals/{lesson.id}/decide",
            json={"decision": "approve", "text": "always check the sample count"},
        )
        assert ok.status_code == 200 and ok.json()["state"] == "approved"
        assert ok.json()["proposed_text"] == "always check the count"
        again = c.post(f"/api/proposals/{lesson.id}/decide", json={"decision": "reject"})
        assert again.status_code == 400 and "already approved" in again.json()["error"]
        cp = c.post(f"/api/proposals/{r['proposal']}/decide", json={"decision": "approve",
                                                                     "value": "ms"})  # fmt: skip
        assert cp.status_code == 200 and cp.json()["edited"] is True
        assert c.post("/api/proposals/x9/decide", json={"decision": "reject"}).status_code == 404
        assert c.get("/api/proposals/summary").json() == {"lessons": {"default": 1}, "pending": 0}
        ref = c.post(f"/api/lessons/{lesson.id}/refute", json={"reason": "new arch"})
        assert ref.status_code == 200 and ref.json()["state"] == "refuted"
    claim = svc.ws.catalog_entry("default", "checkout_latency").fields["unit"]
    assert (claim.origin, claim.value) == ("user", "ms")


def test_user_decisions_reach_claude_as_channel_text(svc):
    f1 = finding(svc, "checkout_latency")
    lesson = svc.retro.lesson_propose("check counts", LessonScope(source="default"), [f1])
    svc.retro.decide_lesson(lesson.id, "approve", "user", text="check sample counts")
    (r,) = svc.retro.catalog_propose(
        "default",
        [{"metric": "checkout_latency", "field": "unit", "value": "s", "confidence": 0.5,
          "basis": "b"}],
    )  # fmt: skip
    svc.retro.decide_proposal(r["proposal"], "reject", "user", comment="it is ms")
    texts = [describe_event(e) for e in svc.log.since(0) if e.klass == "intentional"]
    assert f'user approved (edited) lesson {lesson.id} ("check sample counts")' in texts
    assert any(
        t.startswith(f"user rejected catalog proposal {r['proposal']}") and t.endswith("it is ms")
        for t in texts
    )


def test_ensure_hook_prints_counts_never_lesson_text(monkeypatch, capsys):
    from telemetry_nerd import cli

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"lessons": {"prom": 2}, "pending": 3}

    monkeypatch.setattr(cli.httpx, "get", lambda *a, **k: R())
    lines = cli._retro_lines("http://d")
    assert len(lines) == 2
    assert "prom (2)" in lines[0] and "lessons_for(source, services)" in lines[0]
    assert "3 retrospective proposal(s)" in lines[1]

    R.json = staticmethod(lambda: {"lessons": {}, "pending": 0})
    assert cli._retro_lines("http://d") == []

    def boom(*a, **k):
        raise cli.httpx.ConnectError("down")

    monkeypatch.setattr(cli.httpx, "get", boom)
    assert cli._retro_lines("http://d") == []
