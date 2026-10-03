"""check_littles_law's MCP result is compact by default (zroj).

Eval round 4: check_littles_law(by=["pod"], window="40s", 12 min, 3 pods) returned 54,310
characters; Claude Code saved it to a file the unattended agent could not read, and the
per-pod breakdown was lost. Bulk data never enters Claude's context: the default view keeps
totals, flagged groups and windows in full and every other group as one row (top 10, rest
counted)."""

import asyncio
import json

from mcp import Client

from telemetry_nerd.core.littles_compact import MAX_GROUP_ROWS, compact, flagged, group_name
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.workspace.models import FindingIn

from .fakes import NOW, make_service
from .littles_sim import ARRIVALS, CONCURRENCY, LATENCY, SimSource, simulate

START = NOW - 3_600_000
ROLES = {"arrival_rate": ARRIVALS, "latency": LATENCY, "concurrency": CONCURRENCY}
BUDGET = 6_000  # characters for everything but the flagged groups' own detail
#: with a flagged total (systematic offset, its hints and evidence) the top says more
FLAGGED_TOTAL_BUDGET = 7_000


def _svc(tmp_path, n: int, bad: str | None = None):
    sims = {f"i{i}": simulate(20 + i, rates=[(0.0, 6.0)], c=10) for i in range(n)}
    if bad:  # the latency timer starts at service, not arrival: queueing is outside W (L_high)
        sims[bad] = simulate(7, rates=[(0.0, 3.6)], c=4, timer="service_start")
    return make_service(tmp_path, source=SimSource(sims, START))


async def _mcp(svc, **kw) -> tuple[str, dict]:
    async with Client(build_mcp(svc, "http://x")) as c:
        r = await c.call_tool("check_littles_law", {
            "start": "now-1h", "end": "now", "window": "5m", "by": ["instance"], **ROLES, **kw,
        })  # fmt: skip
    text = r.content[0].text  # type: ignore[union-attr]
    return text, json.loads(text)


def _size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")))  # as the MCP sends it


def _unflagged_size(out: dict) -> int:
    return _size({k: v for k, v in out.items() if k != "flagged_groups"})


def test_fifty_groups_fit_and_the_flagged_one_is_never_cut(tmp_path):
    svc = _svc(tmp_path, 50, bad="i7")
    _, out = asyncio.run(_mcp(svc))
    full = asyncio.run(svc.check_littles_law(start="now-1h", end="now", window="5m",
                                             by=["instance"], **ROLES))  # fmt: skip
    assert _size(full) > 200_000  # what the agent got before
    assert out["verdict"] != "consistent"  # i7's offset shows in the total too
    assert _unflagged_size(out) < FLAGGED_TOTAL_BUDGET, _unflagged_size(out)
    marked = [g for g in full["groups"] if flagged(g)]
    assert any(g["labels"] == {"instance": "i7"} for g in marked)
    # every flagged group: in full (flagged windows, classification, evidence), listed first
    names = [group_name(g["labels"]) for g in marked]
    assert {group_name(g["labels"]) for g in out["flagged_groups"]} == set(names)
    bad = next(g for g in out["flagged_groups"] if g["labels"] == {"instance": "i7"})
    src = next(g for g in full["groups"] if g["labels"] == {"instance": "i7"})
    assert bad["verdict"] == src["verdict"] != "consistent"
    assert bad["flagged_windows"] == src["flagged_windows"]
    assert [e["name"] for e in bad["evidence"]] == [e["name"] for e in src["evidence"]]
    assert {r[0] for r in out["groups"][: len(marked)]} == set(names)
    # the table: at most 10 rows unless more are flagged; the rest counted, never silently
    assert len(out["groups"]) == max(MAX_GROUP_ROWS, len(marked))
    assert out["other_groups"]["count"] == 50 - len(out["groups"])
    assert sum(out["other_groups"]["verdicts"].values()) == out["other_groups"]["count"]
    assert out["group_columns"][0] == "group" and "detail=true" in out["detail"]
    # the total keeps its flagged windows and evidence; consistent windows' rows are left out
    assert out["total"]["flagged_windows"] == full["total"]["flagged_windows"]
    assert "windows" not in out["total"] and "method" not in out


def test_fifty_consistent_groups_fit_the_budget(tmp_path):
    svc = _svc(tmp_path, 50)
    text, out = asyncio.run(_mcp(svc))
    assert _unflagged_size(out) < BUDGET, _unflagged_size(out)
    assert len(text) < BUDGET + sum(_size(g) for g in out.get("flagged_groups", []))
    assert len(out["groups"]) + out["other_groups"]["count"] == 50


def test_round4_three_pods_short_windows_stay_readable(tmp_path):
    # the run's shape: 3 groups, many short windows (40 s over 12 min there; 2 m over 1 h here)
    svc = _svc(tmp_path, 3)
    _, out = asyncio.run(_mcp(svc, window="2m"))
    full = asyncio.run(svc.check_littles_law(start="now-1h", end="now", window="2m",
                                             by=["instance"], **ROLES))  # fmt: skip
    assert _size(full) > 40_000
    assert _unflagged_size(out) < BUDGET
    assert len(out["groups"]) == 3  # every pod is in the table
    for g in out.get("flagged_groups", []):
        src = next(x for x in full["groups"] if x["labels"] == g["labels"])
        assert g["flagged_windows"] == src["flagged_windows"]


def test_detail_and_group_give_the_full_result(tmp_path):
    svc = _svc(tmp_path, 12)
    _, full = asyncio.run(_mcp(svc, detail=True))
    assert len(full["groups"]) == 12 and "windows" in full["groups"][0] and "method" in full
    _, one = asyncio.run(_mcp(svc, group="instance=i3"))
    assert one["group"]["labels"] == {"instance": "i3"} and "windows" in one["group"]
    assert len(one["groups"]) <= 12


def test_compact_evidence_is_citable_with_its_source(tmp_path):
    svc = _svc(tmp_path, 2)
    _, out = asyncio.run(_mcp(svc))
    ev = out["total"]["evidence"][0]
    assert ev["method"].startswith("check_littles_law: ") and len(ev["method"]) < 120
    ev = {k: v for k, v in ev.items() if k != "source"}  # the agent drops it: found again
    f = svc.ws.finding_create(
        FindingIn.model_validate({
            "claim": "L matches lambda W", "evidence": [ev],
            "scope": {"source": "default", "selector": CONCURRENCY,
                      "time_range": {"start_ms": START + 300_000, "end_ms": NOW},
                      "step": "15s", "aggregation": "sum"},
        }),
        "claude",
    )  # fmt: skip
    assert f.sources == ["measurement_system"]


def test_unknown_group_is_refused_with_the_names():
    out = {"total": {"evidence": []}, "groups": [{"labels": {"pod": "a"}, "verdict": "consistent",
                                                  "ratio": 1.0}]}  # fmt: skip
    try:
        compact(out, "pod=b")
    except ValueError as e:
        assert "pod=a" in str(e)
    else:
        raise AssertionError("no refusal")
