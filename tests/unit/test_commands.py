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
EXPECTED = {"start", "connect", "investigate", "open", "learn"}
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
