"""The Charts workspace: ad-hoc graphs placed apart from the active investigation."""

from __future__ import annotations

import json

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.workspace.models import GapIn

from .fakes import NOW, make_service
from .test_mcp import call, text_of

SERIES = [{"label": "cats", "ts": [NOW - 60_000, NOW], "values": [1, 4]}]


def _charts_id(svc) -> str:
    return svc.registry.ensure_charts().id


async def test_charts_target_draws_on_charts_page_and_keeps_active_workspace(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    active = svc.active.active
    r = await call(mcp, "plot_data", {"question": "Cats?", "series": SERIES, "target": "charts"})
    assert not r.is_error, text_of(r)
    out = json.loads(text_of(r))
    assert out["url"] == f"http://x/#/charts/{out['panel']}"
    assert svc.active.active == active
    assert svc.ws.snapshot()["panels"] == []
    with svc.active.using(_charts_id(svc)):
        assert [p["id"] for p in svc.ws.snapshot()["panels"]] == [out["panel"]]
    assert svc.registry.get(_charts_id(svc)).kind == "charts"


async def test_charts_page_lists_newest_panel_first_across_all_ad_hoc_tools(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    await call(mcp, "query", {"expr": "up", "start": "now-2h", "end": "now-1h"})
    shown = await call(mcp, "show", {"dataset": "d1", "question": "Up?", "target": "charts"})
    csv = await call(
        mcp, "plot_csv", {"question": "Csv?", "csv": "ts,a\n1000,1\n2000,2\n", "target": "charts"}
    )
    assert not shown.is_error and not csv.is_error, text_of(shown) + text_of(csv)
    assert json.loads(text_of(shown))["url"].startswith("http://x/#/charts/")
    with svc.active.using(_charts_id(svc)):
        assert [p["question"] for p in svc.ws.snapshot()["panels"]] == ["Csv?", "Up?"]
    assert len(svc.registry.list()) == 2  # w1 and the one Charts workspace


async def test_default_target_still_draws_in_the_active_workspace(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    r = await call(mcp, "plot_data", {"question": "Cats?", "series": SERIES})
    assert json.loads(text_of(r))["url"] == "http://x/#/panel/p1"
    assert [p["id"] for p in svc.ws.snapshot()["panels"]] == ["p1"]
    assert svc.registry.list() and all(w.kind == "investigation" for w in svc.registry.list())


async def test_findings_hypotheses_and_gaps_are_refused_on_the_charts_page(tmp_path):
    svc = make_service(tmp_path)
    await svc.workspaces.switch(_charts_id(svc), "user")
    for call_it in (
        lambda: svc.ws.hypothesis_create("db is slow", "claude"),
        lambda: svc.ws.gap_create(GapIn(missing_signal="x", needed_for="y"), "claude"),
    ):
        try:
            call_it()
        except ValueError as e:
            assert "workspace_promote" in str(e)
        else:
            raise AssertionError("expected a refusal on the Charts page")


async def test_charts_workspace_cannot_be_archived(tmp_path):
    svc = make_service(tmp_path)
    cid = _charts_id(svc)
    try:
        svc.workspaces.update(cid, archived=True, actor="user")
    except ValueError as e:
        assert "cannot be archived" in str(e)
    else:
        raise AssertionError("expected a refusal")


async def test_promote_opens_an_investigation_seeded_with_the_panel(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    r = await call(mcp, "plot_data", {"question": "Cats?", "series": SERIES, "target": "charts"})
    pid = json.loads(text_of(r))["panel"]
    promoted = await call(mcp, "workspace_promote", {"panel": pid})
    assert not promoted.is_error, text_of(promoted)
    out = json.loads(text_of(promoted))
    assert out["workspace"]["question"] == "Cats?" and svc.active.active == out["workspace"]["id"]
    (copy,) = svc.ws.snapshot()["panels"]
    assert copy["id"] == out["panel"] != pid
    assert copy["question"] == "Cats?"
    original = svc.workspace.get_panel(pid)
    assert copy["dataset_ids"] == original.dataset_ids and not original.closed
    assert svc.registry.get(svc.active.active).kind == "investigation"


async def test_promote_refuses_a_panel_outside_the_charts_page(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    r = await call(mcp, "plot_data", {"question": "Cats?", "series": SERIES})
    promoted = await call(mcp, "workspace_promote", {"panel": json.loads(text_of(r))["panel"]})
    assert promoted.is_error and "not on the Charts page" in text_of(promoted)
