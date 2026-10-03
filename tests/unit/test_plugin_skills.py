"""The evidence, triage and charting skills (d77.2): the worked triage example runs end to end
through the MCP path on a seeded scenario (slow), and every tool, argument, mark and y-view the
prose names exists.

Calls are `json` blocks right below `<!-- call: scenario=<name> id=<id> [expect=error] -->`; they
run in order per scenario on a fresh service. A string argument `"$<id>.<path>"` is replaced by
that value of an earlier call's result (path parts are keys or list indices)."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, get_args

import pytest
from mcp import Client

from telemetry_nerd.charts.spec import Mark
from telemetry_nerd.charts.yview import YMode
from telemetry_nerd.mcp.server import INSTRUCTIONS, build_mcp
from tests.unit.demo_source import DemoSource
from tests.unit.fakes import make_service
from tests.unit.test_verdicts import ScenarioSource

ROOT = Path(__file__).resolve().parents[2]
SKILLS = {name: ROOT / "skills" / name for name in ("evidence", "triage", "charting")}
CALL = re.compile(r"<!--\s*call:\s*(?P<attrs>[^>]*?)\s*-->\s*```json\n(?P<body>.*?)```", re.DOTALL)
SIG = re.compile(r"`(\w+)\(([^`]*)\)`")
REF = re.compile(r"^\$(\w+)((?:\.[\w]+)*)$")
# words in `name(...)` form that are PromQL functions or the tn API, not tools
NOT_TOOLS = {"rate", "sum", "increase", "histogram_quantile", "histogram_count", "histogram_sum",
             "avg_over_time", "max", "avg", "dataset", "put"}  # fmt: skip


def md_files(skill: str | None = None) -> list[Path]:
    roots = [SKILLS[skill]] if skill else list(SKILLS.values())
    return sorted(p for r in roots for p in r.rglob("*.md"))


def calls() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for path in md_files():
        for m in CALL.finditer(path.read_text()):
            attrs = dict(a.split("=", 1) for a in m["attrs"].split())
            call = json.loads(m["body"])
            out[attrs["scenario"]].append(
                {**call, "id": attrs["id"], "expect": attrs.get("expect", "ok")}
            )
    return out


CALLS = calls()


def resolve(value: Any, results: dict[str, Any]) -> Any:
    """Replace `$id.path` strings by the value at that path of an earlier result."""
    if isinstance(value, dict):
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    if isinstance(value, str) and (m := REF.match(value)):
        node = results[m[1]]
        for part in m[2].split(".")[1:]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        return node
    return value


def scenario_source(scenario: str):
    if scenario == "triage":
        return ScenarioSource(seed=3, latency_shift=20, error_burst=(40, 47))
    assert scenario == "payment"
    return DemoSource()


async def run(tmp_path, scenario: str) -> dict[str, Any]:
    svc = make_service(tmp_path, scenario_source(scenario))
    results: dict[str, Any] = {}
    async with Client(build_mcp(svc, "http://x")) as c:
        for call in CALLS[scenario]:
            r = await c.call_tool(call["tool"], resolve(call["args"], results))
            text = r.content[0].text
            assert r.is_error == (call["expect"] == "error"), (call["id"], text)
            results[call["id"]] = text if r.is_error else json.loads(text)
        results["_workspace"] = json.loads((await c.call_tool("workspace_get", {})).content[0].text)
    return results


async def tool_schemas(tmp_path) -> dict[str, dict]:
    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        return {t.name: t.input_schema for t in (await c.list_tools()).tools}


# --- the worked triage example runs ---------------------------------------------------------------
@pytest.mark.slow
async def test_triage_example_symptom_to_finding(tmp_path):
    r = await run(tmp_path, "triage")
    assert r["existing"]["bindings"] == []
    assert r["suggest"]["suggestions"][0]["id"] == "RED:otel_http"
    g = r["group"]
    assert g["group"] == "pg1" and g["roles"]["duration"]["view"] == "heatmap"
    # the verdict the prose reads
    v = r["verdict"]
    assert v["family"]["alpha"] == 0.05 and v["family"]["roles_judged"] == 3
    assert v["reference"]["cycles"] == 4
    roles = v["roles"]
    d, e, rate = roles["duration"], roles["errors"], roles["rate"]
    assert (d["pattern"], d["source"]) == ("shift", "special_cause")
    assert d["onset"]["at"].endswith("10:00:00+00:00")
    assert (
        d["onset"]["interval"][0][11:16] == "09:56" and d["onset"]["interval"][1][11:16] == "10:02"
    )
    assert round(d["threshold"]["reference_share"], 3) == 0.012
    assert round(d["threshold"]["now_share"], 3) == 0.054
    assert (e["pattern"], e["source"]) == ("burst", "special_cause")
    assert (
        e["onset"]["interval"][0][11:16] == "10:17" and e["onset"]["interval"][1][11:16] == "10:22"
    )
    assert v["summary"]["first"] == "duration"
    assert (rate["status"], rate["source"]) == ("no_change", "common_cause")
    lo, hi = rate["evidence"][0]["interval"]
    assert lo < 1 < hi and round(rate["evidence"][0]["value"], 2) == 0.99
    # blast radius: every member unusual for this hour of the day
    s = r["seasonal"]["series"]
    assert sorted(x["labels"]["service_name"] for x in s) == ["s0", "s1"]
    assert all((x["verdict"], x["direction"], x["source"]) == ("unusual", "higher", "special_cause")
               for x in s)  # fmt: skip
    # hypotheses ruled out with findings against them, the main finding with its sources
    assert r["h_load_refuted"]["status"] == r["h_daily_refuted"]["status"] == "refuted"
    assert r["f_load"]["sources"] == ["common_cause"]
    assert r["f_daily"]["sources"] == ["special_cause"]
    assert r["f_main"]["sources"] == ["special_cause"] and "uncertainty" not in r["f_main"]
    ws = r["_workspace"]
    statuses = {h["id"]: h["status"] for h in ws["hypotheses"]}
    assert statuses == {"h1": "refuted", "h2": "refuted", "h3": "proposed"}
    assert sorted(f["id"] for f in ws["findings"]) == ["f1", "f2", "f3", "f4"]
    ids = [r[k]["finding"] for k in ("f_load", "f_daily", "f_main", "f_errors")]
    assert ids == ["f1", "f2", "f3", "f4"] and r["f_errors"]["sources"] == ["special_cause"]


@pytest.mark.slow
async def test_discovery_example_finds_the_root_cause_service(tmp_path):
    """rbz/t75: services first (entities), RED from span metrics over all of them, then a cause
    hypothesis for the episode with a refuted competitor."""
    r = await run(tmp_path, "payment")
    assert r["search"]["results"][0]["metric"].startswith("http_server_request_duration_seconds")
    by = {e["value"]: e for e in r["services"]["entities"]}
    assert len(by) == 15 and "payment" in by
    pay = by["payment"]
    assert "http_server" not in pay["families"] and "rpc_server" not in pay["families"]
    assert "RED:spanmetrics" in pay["bindings"]
    assert pay["next"] == 'binding_suggest(kind="RED", key="payment")'
    assert by["quote"]["active_recent"] is False
    top = r["suggest"]["suggestions"][0]
    assert (top["id"], top["key"]) == ("RED:spanmetrics", "payment")
    failing = sorted(s["labels"]["service_name"] for s in r["errors"]["summary"]["series"])
    assert failing == ["checkout", "frontend", "payment"]
    err = r["an"]["series"][1]
    assert err["labels"] == {"status_code": "STATUS_CODE_ERROR"}
    assert err["verdict"] == "level_shifted" and r["an"]["series"][0]["verdict"] == "stable"
    assert err["stability"]["shifts"][0]["at"].endswith("10:15:00+00:00")
    assert r["f_cause"]["sources"] == ["special_cause"] and "hint" not in r["f_cause"]
    assert r["f_cause"]["scope"]["status"] == "covered"
    assert r["f_alt"]["scope"]["status"] == "covered"
    statuses = {h["id"]: h["status"] for h in r["_workspace"]["hypotheses"]}
    assert statuses == {"h1": "supported", "h2": "refuted"}


def test_discovery_example_has_the_flow():
    tools = [c["tool"] for c in CALLS["payment"]]
    # services before the blast radius; a cause hypothesis only once the episode is found
    assert tools.index("entities") < tools.index("query")
    assert tools.index("binding_suggest") < tools.index("query")
    assert tools.index("annotate") < tools.index("hypothesis_create")
    assert tools.count("hypothesis_create") >= 2
    assert tools[-2:] == ["hypothesis_update", "hypothesis_update"]


def test_triage_example_has_the_flow():
    tools = [c["tool"] for c in CALLS["triage"]]
    assert set(CALLS) == {"triage", "payment"}
    for t in ("binding_suggest", "binding_accept", "show_binding", "binding_verdict",
              "query_distribution", "compare_seasonal", "hypothesis_create", "finding_create",
              "hypothesis_update", "annotate"):  # fmt: skip
        assert t in tools, t
    assert tools.index("binding_verdict") < tools.index("finding_create")
    assert len({c["id"] for c in CALLS["triage"]}) == len(CALLS["triage"])


# --- the prose names real tools, arguments, marks and y-views ------------------------------------
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


@pytest.mark.parametrize("skill", sorted(SKILLS))
async def test_skill_names_real_tools_and_arguments(tmp_path, skill):
    schemas = await tool_schemas(tmp_path)
    for path in md_files(skill):
        for name, args in SIG.findall(path.read_text()):
            if name in NOT_TOOLS:
                continue
            assert name in schemas, (path.name, name)
            props = set(schemas[name]["properties"])
            for part in parts(args):
                m = re.match(r"\s*(\w+)\s*(=|$)", part)
                if m:
                    assert m[1] in props, (path.name, name, m[1], sorted(props))


def test_json_calls_have_valid_arguments(tmp_path):
    schemas = asyncio.run(tool_schemas(tmp_path))
    for scenario in CALLS.values():
        for c in scenario:
            sch = schemas[c["tool"]]
            assert set(c["args"]) <= set(sch["properties"]), c
            assert set(sch.get("required", [])) <= set(c["args"]), c


def test_backticked_tool_names_exist(tmp_path):
    """Backticked snake_case words that look like tool names are tools."""
    schemas = asyncio.run(tool_schemas(tmp_path))
    prefixes = ("binding_", "catalog_", "source_", "hypothesis_", "finding_", "gap_",
                "workspace_", "show_", "query_", "check_", "compare_", "operating_", "split_",
                "suggest_", "set_", "fraction_", "run_", "code_")  # fmt: skip
    for skill in SKILLS:
        text = "".join(p.read_text() for p in md_files(skill))
        named = {w for w in re.findall(r"`([a-z_]+)`", text) if w.startswith(prefixes)}
        assert named <= set(schemas), (skill, named - set(schemas))


def test_marks_and_y_views_exist():
    marks = set(get_args(Mark)) | {"auto"}
    modes = set(get_args(YMode))
    text = "".join(p.read_text() for p in md_files())
    for m in re.findall(r'mark="(\w[\w+]*)"', text):
        assert m in marks, m
    for m in re.findall(r'mode="(\w+)"', text):
        assert m in modes, m
    charting = "".join(p.read_text() for p in md_files("charting"))
    for m in marks - {"line+envelope"}:  # the default line is `auto`
        assert f'mark="{m}"' in charting, m


def test_hypothesis_statuses_and_sources_are_the_wire_values():
    from telemetry_nerd.workspace.models import HypothesisStatus

    text = (SKILLS["evidence"] / "SKILL.md").read_text()
    for st in get_args(HypothesisStatus):
        assert f"`{st}`" in text, st
    for src in ("common_cause", "special_cause", "measurement_system", "undetermined"):
        assert f"`{src}`" in text, src


@pytest.mark.parametrize("skill", sorted(SKILLS))
def test_skill_structure_and_frontmatter(skill):
    d = SKILLS[skill]
    text = (d / "SKILL.md").read_text()
    head = text.split("---")[1]
    assert f"name: {skill}" in head and "description: This skill should be used when" in head
    words = len(text.split("---", 2)[2].split())
    assert words < {"triage": 1400}.get(skill, 2200), words
    refs = re.findall(r"`(references/[\w.-]+)`", text)
    assert refs
    for ref in refs:
        assert (d / ref).is_file(), ref
    assert {f"references/{p.name}" for p in (d / "references").glob("*.md")} <= set(refs)
    # sibling skills are pointed to, not duplicated
    if skill == "triage":
        for other in ("model-views", "metric-learning", "tier2-code", "evidence", "charting"):
            assert f"`{other}`" in text, other


def test_instructions_point_to_the_skills():
    flat = " ".join(INSTRUCTIONS.split())
    for skill in SKILLS:
        assert f"`{skill}` skill" in flat, skill
