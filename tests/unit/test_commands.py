"""Plugin commands (d77.1): frontmatter parses, every MCP tool a command allows or names exists
on the server, and the skills a command defers to are either present or on the agreed list."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from mcp import Client

from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import make_service

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = sorted((ROOT / "commands").glob("*.md"))
PREFIX = "mcp__plugin_telemetry-nerd_telemetry-nerd__"
EXPECTED = {"start", "connect", "investigate", "open", "learn", "wrap"}
# Skills written in parallel (d77.2); commands may reference them before the files land.
PLANNED_SKILLS = {"triage", "evidence", "charting"}


def parse(path: Path) -> tuple[dict, str]:
    m = re.match(r"---\n(.*?)\n---\n(.*)", path.read_text(), re.DOTALL)
    assert m, f"{path.name}: no frontmatter"
    return yaml.safe_load(m[1]), m[2]


@pytest.fixture
async def tools(tmp_path):
    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        return {t.name for t in (await c.list_tools()).tools}


def test_expected_commands_exist():
    assert {p.stem for p in COMMANDS} == EXPECTED


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.stem)
def test_frontmatter(path):
    fm, body = parse(path)
    assert fm["description"] and len(fm["description"]) <= 100
    assert "allowed-tools" in fm
    if "$ARGUMENTS" in body:
        assert fm.get("argument-hint")


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.stem)
async def test_tools_exist(path, tools):
    fm, body = parse(path)
    allowed = [t.strip() for t in fm["allowed-tools"].split(",")]
    mcp_allowed = {t.removeprefix(PREFIX) for t in allowed if t.startswith("mcp__")}
    assert all(t.startswith(PREFIX) for t in allowed if t.startswith("mcp__"))
    assert mcp_allowed <= tools, mcp_allowed - tools
    named = set(re.findall(r"`(\w+)\(", body)) | set(re.findall(r"`(\w+)`", body))
    used = {n for n in named if n in tools}
    assert used <= mcp_allowed, f"{path.stem} names tools it may not call: {used - mcp_allowed}"


@pytest.mark.parametrize("path", COMMANDS, ids=lambda p: p.stem)
def test_skills_referenced_exist(path):
    _, body = parse(path)
    for name in re.findall(r"`(\w[\w-]*)` skill", body):
        assert (ROOT / "skills" / name / "SKILL.md").exists() or name in PLANNED_SKILLS, name


def test_investigate_annotates_and_can_call_what_triage_uses():
    """Eval finding (pxu): the command never annotated and could not call annotate/analyze/..."""
    fm, body = parse(ROOT / "commands" / "investigate.md")
    allowed = {t.strip().removeprefix(PREFIX) for t in fm["allowed-tools"].split(",")}
    triage = (ROOT / "skills" / "triage" / "SKILL.md").read_text()
    needed = {"annotate", "analyze", "run_code", "code_get", "catalog_family", "catalog_search",
              "compare_seasonal", "fleet", "check_littles_law", "binding_verdict", "show_binding",
              "binding_suggest", "binding_accept", "hypothesis_update", "gap_create"}  # fmt: skip
    assert needed <= allowed, needed - allowed
    assert "annotate(" in body and 'kind="region"' in body
    for name in ("analyze", "compare_seasonal", "fleet", "binding_verdict", "check_littles_law"):
        assert name in triage and name in allowed


def test_investigate_states_claim_scope_and_supported_rules():
    """bvx: the command tells Claude what finding_create / hypothesis_update now enforce."""
    _, body = parse(ROOT / "commands" / "investigate.md")
    flat = " ".join(body.split())
    for word in ("scope_note", "alternatives_considered", "source_flags", "`scope`",
                 "concrete subject", "stance=for"):  # fmt: skip
        assert word in flat, word


def _allowed(name: str) -> set[str]:
    fm, _ = parse(ROOT / "commands" / f"{name}.md")
    return {t.strip().removeprefix(PREFIX) for t in fm["allowed-tools"].split(",")}


def test_start_uses_the_grafana_front_door_and_surfaces_lessons():
    """3fs.2 shipped: a Grafana URL goes through discovery and connect-by-uid, not a hand-built
    proxy url; approved lessons are asked for once the source is known (3fs.4)."""
    _, body = parse(ROOT / "commands" / "start.md")
    flat = " ".join(body.split())
    assert {"source_discover_grafana", "source_connect", "lessons_for"} <= _allowed("start")
    assert "source_discover_grafana(url)" in flat
    assert "grafana=<url>, uid=<uid>" in flat and "supported" in flat
    assert "not built yet" not in flat and "proxy/uid" not in flat


def test_wrap_proposes_and_never_decides():
    _, body = parse(ROOT / "commands" / "wrap.md")
    flat = " ".join(body.split())
    allowed = _allowed("wrap")
    assert {"catalog_propose", "lesson_propose", "proposals_list", "workspace_get"} <= allowed
    assert "catalog_write" not in allowed  # proposals only; the user decides
    for word in ("lesson_beyond_evidence", "#/proposals", "rejected finding is not evidence"):
        assert word in flat, word


def test_investigate_asks_for_lessons_in_scope():
    _, body = parse(ROOT / "commands" / "investigate.md")
    assert {"lessons_for", "lesson_refute"} <= _allowed("investigate")
    assert "lessons_for(source, services=" in body and "/telemetry-nerd:wrap" in body
