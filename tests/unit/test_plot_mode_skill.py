"""The plot-mode skill (rnkq.12): the tools it names are registered, and its pie rule and limits
match the code."""

from __future__ import annotations

import re
from pathlib import Path

from mcp import Client

from telemetry_nerd.core import plot_table
from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import make_service

SKILL = Path(__file__).resolve().parents[2] / "skills" / "plot-mode" / "SKILL.md"
PLOT_TOOLS = {"plot_data", "plot_csv"}


async def test_plot_mode_skill_cites_only_registered_tools(tmp_path):
    async with Client(build_mcp(make_service(tmp_path), "http://x")) as c:
        registered = {t.name for t in (await c.list_tools()).tools}
    text = SKILL.read_text()
    cited = {
        w for w in re.findall(r"`(\w+)(?:\(|`)", text) if w in registered or w.startswith("plot_")
    }
    assert PLOT_TOOLS <= cited <= registered, cited - registered
    assert {"query", "show"} <= registered


def test_plot_mode_skill_states_the_code_limits():
    body = SKILL.read_text()
    assert f"| Bars | {plot_table.MAX_BARS} |" in body
    assert (
        f"| Points per time series | {plot_table.MAX_POINTS_PER_TIME_SERIES:,} |".replace(",", " ")
        in body
    )
    assert f"| Values per sample | {plot_table.MAX_VALUES_PER_SAMPLE:,} |".replace(",", " ") in body
    assert f"| Label length | {plot_table.MAX_LABEL_CHARS} characters |" in body
    assert f"{plot_table.MAX_CSV_BYTES // 1024 // 1024} MiB" in body
    assert "`refused_form`" in body and 'kind="bar"' in body


def test_plot_mode_skill_frontmatter():
    head = SKILL.read_text().split("---")[1]
    assert "name: plot-mode" in head and "description: This skill should be used when" in head
