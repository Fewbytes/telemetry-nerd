"""The model-views skill (czt.5): every tool call in its worked examples runs through the MCP
path on seeded data (slow), and every tool and argument its prose names exists.

Calls are `json` blocks right below `<!-- call: scenario=<name> [expect=error] -->`; they run in
order per scenario on a fresh service. The scenarios are the fixtures of the verdict (czt.4) and
Little's law (czt.2) tests, so the numbers the examples state are asserted below."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest
from mcp import Client

from telemetry_nerd.mcp.server import INSTRUCTIONS, build_mcp
from telemetry_nerd.model.time import iso
from tests.unit.fakes import NOW, make_service
from tests.unit.littles_sim import SimSource, simulate
from tests.unit.test_verdicts import ScenarioSource

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "model-views"
CALL = re.compile(r"<!--\s*call:\s*(?P<attrs>[^>]*?)\s*-->\s*```json\n(?P<body>.*?)```", re.DOTALL)
SIG = re.compile(r"`(\w+)\(([^`]*)\)`")
START = NOW - 3_600_000


def md_files() -> list[Path]:
    return sorted(SKILL.rglob("*.md"))


def calls() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for path in md_files():
        for m in CALL.finditer(path.read_text()):
            attrs = dict(a.split("=", 1) for a in m["attrs"].split())
            call = json.loads(m["body"])
            out[attrs["scenario"]].append({**call, "expect": attrs.get("expect", "ok")})
    return out


CALLS = calls()


async def make(tmp_path, scenario: str):
    if scenario == "red":
        svc = make_service(tmp_path, ScenarioSource(seed=3, latency_shift=20, error_burst=(40, 47)))
        await svc.learn("default")
        return svc
    if scenario == "littles-spike":  # rho 1.25 for minutes 25-30, an arrivals counter
        sims = {"i0": simulate(602, rates=[(0.0, 2.0), (1500.0, 5.0), (1800.0, 2.0)], c=4)}
        return make_service(tmp_path, source=SimSource(sims, START))
    timer = "arrival" if scenario == "littles-ok" else "service_start"
    sims = {"i0": simulate(5, rates=[(0.0, 9.5)], c=10, timer=timer)}
    return make_service(tmp_path, source=SimSource(sims, START))


async def run(tmp_path, scenario: str) -> list[tuple[dict, bool, str]]:
    svc = await make(tmp_path, scenario)
    out = []
    async with Client(build_mcp(svc, "http://x")) as c:
        for call in CALLS[scenario]:
            r = await c.call_tool(call["tool"], call["args"])
            out.append((call, r.is_error, r.content[0].text))
    return out


def results(out):
    """tool name -> parsed JSON (successful calls) in order."""
    return [(c["tool"], json.loads(t) if not err else t) for c, err, t in out]


async def tool_schemas(tmp_path) -> dict[str, dict]:
    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        return {t.name: t.input_schema for t in (await c.list_tools()).tools}


# --- the examples run -----------------------------------------------------------------------------
pytestmark = pytest.mark.slow


def test_every_scenario_has_calls():
    assert set(CALLS) == {"red", "littles-ok", "littles-queue", "littles-spike"}
    assert [c["tool"] for c in CALLS["red"]] == [
        "binding_suggest", "binding_accept", "show_binding", "binding_verdict",
    ]  # fmt: skip


async def test_red_example_ordered_verdict(tmp_path):
    out = await run(tmp_path, "red")
    for call, err, text in out:
        assert err is False, (call["tool"], text)
    suggest, accept, show, verdict = (r for _, r in results(out))
    s = suggest["suggestions"][0]
    assert s["id"] == "RED:otel_http" and s["key"] == "http.server" and s["unfilled"] == []
    assert s["join_on"] == ["service_name"] and s["ambiguous_roles"] == ["rate", "errors"]
    assert (
        s["detail"]["duration"]["confidence"] == 0.9 and not s["detail"]["duration"]["alternatives"]
    )
    assert accept["effective"] and accept["binding"]["key"] == "http.server"
    assert show["group"] == "pg1" and show["gaps"] == []
    roles = show["roles"]
    assert (roles["rate"]["panel"], roles["errors"]["panel"], roles["duration"]["panel"]) == (
        "p1", "p2", "p3",
    )  # fmt: skip
    assert roles["errors"]["form"] == "error_ratio" and roles["duration"]["view"] == "heatmap"
    assert roles["errors"]["notes"]
    # the verdict the prose reports
    assert verdict["group"] == "pg1" and verdict["family"]["alpha"] == 0.05
    assert verdict["family"]["roles_judged"] == 3
    assert (
        verdict["reference"]["label"] == "previous time ranges"
        and verdict["reference"]["cycles"] == 4
    )
    r = verdict["roles"]
    assert r["rate"]["status"] == "no_change"
    d, e = r["duration"], r["errors"]
    assert (d["status"], d["pattern"], d["direction"]) == ("changed", "shift", "higher")
    assert (e["status"], e["pattern"], e["direction"]) == ("changed", "burst", "higher")
    assert d["onset"]["at"].endswith("10:00:00+00:00")
    assert (
        d["onset"]["interval"][0][11:16] == "09:56" and d["onset"]["interval"][1][11:16] == "10:02"
    )
    assert e["onset"]["at"].endswith("10:20:00+00:00")
    assert (
        e["onset"]["interval"][0][11:16] == "10:17" and e["onset"]["interval"][1][11:16] == "10:22"
    )
    assert d["onset"]["interval"][1] < e["onset"]["interval"][0]  # no overlap: ordering claimed
    assert verdict["summary"]["first"] == "duration"
    assert verdict["summary"]["moved"] == ["duration", "errors"]
    th = d["threshold"]
    assert th["x"] == 0.25 and round(th["reference_share"], 3) == 0.012
    assert round(th["now_share"], 3) == 0.054
    assert "noisier_than_reference" in e["caveats"] and "overdispersed" in d["caveats"]
    assert e["evidence"] and all(x["kind"] == "statistic" for x in e["evidence"])


async def test_binding_key_is_not_the_suggestion_id(tmp_path):
    """The skill warns that show_binding takes the key, only `suggestion=` takes the id."""
    svc = await make(tmp_path, "red")
    async with Client(build_mcp(svc, "http://x")) as c:
        await c.call_tool(
            "binding_accept", {"source": "default", "id": "RED:otel_http", "basis": "t"}
        )
        r = await c.call_tool(
            "show_binding",
            {"source": "default", "kind": "RED", "key": "otel_http", "range": "1h"},
        )
    assert r.is_error and "bound: RED:http.server" in r.content[0].text


async def test_littles_consistent_example(tmp_path):
    out = await run(tmp_path, "littles-ok")
    assert [err for _, err, _ in out] == [False]
    o = results(out)[0][1]
    assert o["verdict"] == "consistent"
    t = o["total"]
    assert round(t["ratio"], 2) == 0.99 and t["ci95"][0] < 1 < t["ci95"][1]
    d = o["discrepancy"]
    assert round(d["difference"], 2) == -0.16
    assert [round(100 * x) for x in d["relative_ci95"]] == [-6, 5]
    assert [round(x, 2) for x in d["per_window"]["ratio_range"]] == [0.84, 1.16]
    assert (round(t["L"], 1), round(t["lambda_W"], 1), round(t["W_s"], 1)) == (22.9, 23.0, 2.4)
    assert o["classification"]["systematic"] is None and not o["classification"]["transient"]
    cc = t["common_cause"]
    assert round(cc["completions_per_window"]) == 2860 and round(100 * cc["rel95"]) == 8
    assert cc["warning"] is None
    status = {a["name"]: a["status"] for a in o["assumptions"]}
    assert status["arrivals_vs_completions"] == "assumed" and status["warmup"] == "assumed"
    assert {v for k, v in status.items() if k not in ("arrivals_vs_completions", "warmup")} == {
        "ok"
    }


async def test_littles_hidden_queueing_example(tmp_path):
    out = await run(tmp_path, "littles-queue")
    (_, e1, t1), (_, e2, _), (_, e3, t3) = out
    assert (e1, e2, e3) == (False, False, True)
    o = json.loads(t1)
    assert o["verdict"] == "L_high"
    t = o["total"]
    assert round(t["ratio"], 2) == 2.40 and [round(x, 2) for x in t["ci95"]] == [2.27, 2.52]
    assert round(o["discrepancy"]["difference"], 1) == 13.3
    assert round(o["discrepancy"]["lambda_W"], 2) == 9.54
    sysd = o["classification"]["systematic"]
    assert round(sysd["ratio"], 2) == 2.03 and [round(x, 2) for x in sysd["ci95"]] == [1.76, 2.3]
    assert sysd["windows"] == [10, 12] and sysd["source"] == "measurement_system"
    tr = o["classification"]["transient"]
    assert len(tr) == 2 and {(x["source"], x["phase"]) for x in tr} == {("special_cause", "other")}
    assert round(100 * t["common_cause"]["spread_rel"]) == 43
    names = [e["name"] for e in t["evidence"]]
    assert "littles_law_discrepancy" in names and "littles_law_systematic_offset" in names
    assert (round(t["L"], 1), round(t["lambda_per_s"], 2), round(t["W_s"], 1)) == (22.9, 9.55, 1.0)
    assert t["flagged_windows"] and any("queueing before the timer" in h for h in o["hints"])
    assert any("subset" in h for h in o["hints"]) and any("broader" in h for h in o["hints"])
    assert round(t["L"] / t["lambda_per_s"] - t["W_s"], 1) == 1.4  # the implied unmeasured time
    assert o["datasets"]["concurrency"] == "d4"
    assert t["evidence"][0]["name"] == "littles_law_ratio"
    assert "MEAN latency" in t3  # the percentile refusal


async def test_littles_spike_promotion_example(tmp_path):
    out = await run(tmp_path, "littles-spike")
    assert [err for _, err, _ in out] == [False]
    o = results(out)[0][1]
    assert o["verdict"] == "consistent" and not o["classification"]["transient"]
    d = o["discrepancy"]
    assert round(d["difference"], 2) == -0.10 and round(d["ratio"], 3) == 0.997
    assert [round(100 * x) for x in d["relative_ci95"]] == [-3, 3]
    (p,) = o["classification"]["promoted"]
    assert p["window"][0] == iso(START + 1_500_000) and p["source"] == "special_cause"
    assert p["from"] == "measurement_system" and p["deviation"] == "within_measurement"
    (e,) = [e for e in p["evidence"] if e["significant"]]
    assert e["kind"] == "backlog_growth"
    assert (e["value"], e["gauge"], e["flow"]) == (376, 376, 377)
    assert 155 <= e["z"] < 156 and round(e["k"], 1) == 37.9
    assert "backlog grew +376 requests" in p["reason"] and "155× the steady-state" in p["reason"]
    names = [x["name"] for x in o["total"]["evidence"]]
    assert "littles_law_backlog_growth" in names


# --- the prose names real tools and arguments ----------------------------------------------------
def parts(args: str) -> list[str]:
    """Top-level comma / pipe separated pieces of a signature like `a, b=[x, y] | c`."""
    out, depth, cur = [], 0, ""
    for ch in args:
        depth += ch in "[{(<"
        depth -= ch in "]})>"
        if ch in ",|" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + [cur]


async def test_skill_names_real_tools_and_arguments(tmp_path):
    schemas = await tool_schemas(tmp_path)
    ignore = {"tn", "rate", "histogram_quantile", "histogram_count", "sum", "increase"}
    seen = set()
    for path in md_files():
        for name, args in SIG.findall(path.read_text()):
            if name in ignore:
                continue
            assert name in schemas, (path.name, name)
            seen.add(name)
            props = set(schemas[name]["properties"])
            for part in parts(args):
                m = re.match(r"\s*(\w+)\s*(=|$)", part)
                if m:
                    assert m[1] in props, (path.name, name, m[1], sorted(props))
    assert {"binding_suggest", "binding_accept", "show_binding", "binding_verdict",
            "check_littles_law", "catalog_bind", "catalog_relations",             "fleet", "show"} <= seen  # fmt: skip
    # every json call's arguments are valid for the tool: known names, required ones present
    for scenario in CALLS.values():
        for c in scenario:
            sch = schemas[c["tool"]]
            assert set(c["args"]) <= set(sch["properties"]), c
            assert set(sch.get("required", [])) <= set(c["args"]), c


def test_backticked_tool_names_without_parens_exist(tmp_path):
    """Backticked snake_case words in the prose that look like tool names exist as tools."""
    import asyncio

    schemas = asyncio.run(tool_schemas(tmp_path))
    text = "".join(p.read_text() for p in md_files())
    known = set(schemas)
    suffixes = ("_suggest", "_accept", "_verdict", "_binding", "_bind", "_learn", "_create",
                "_relations", "_profile", "_littles_law")  # fmt: skip
    named = {w for w in re.findall(r"`([a-z_]+)`", text) if w.endswith(suffixes)}
    assert named <= known | {"show_binding"}, named - known


def test_skill_structure_and_frontmatter():
    text = (SKILL / "SKILL.md").read_text()
    head = text.split("---")[1]
    assert "name: model-views" in head and "description: This skill should be used when" in head
    words = len(text.split("---", 2)[2].split())
    assert words < 2200, words
    for ref in re.findall(r"`(references/[\w.-]+)`", text):
        assert (SKILL / ref).is_file(), ref
    assert {p.name for p in (SKILL / "references").glob("*.md")} == {
        "binding-workflow.md", "verdicts.md", "littles-law.md", "worked-examples.md",
    }  # fmt: skip


def test_instructions_point_to_the_skill():
    assert "model-views" in " ".join(INSTRUCTIONS.split())
