"""Spec section 4: pie-like forms are refused, never mapped to bar, and exist nowhere else."""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from telemetry_nerd.charts.spec import Mark
from telemetry_nerd.core.plot_table import PIE_LIKE_KINDS, PlotReason
from telemetry_nerd.mcp.server import build_mcp

from .fakes import NOW, make_service
from .test_inline_plot import SERIES
from .test_mcp import call, text_of

ROOT = Path(__file__).parents[2]
FORBIDDEN = re.compile(r"(?<![a-z])(pie|donut|doughnut|sunburst)(?![a-z])", re.IGNORECASE)
CSV = f"ts,a\n{NOW - 1000},1\n{NOW},2\n"

SPELLINGS = ["pie", "PIE", "Donut", "doughnut", "radial", "sunburst", "3d_pie"]
CALLS = {
    "plot_data": {"question": "q?", "series": SERIES},
    "plot_csv": {"question": "q?", "csv": CSV},
}


@pytest.mark.parametrize("tool", CALLS)
@pytest.mark.parametrize("kind", SPELLINGS)
async def test_pie_like_kind_is_refused_and_no_panel_is_created(tmp_path, tool, kind):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    before = len(svc.workspace.list_panels(include_closed=True))
    r = await call(mcp, tool, {**CALLS[tool], "kind": kind})
    assert r.is_error
    assert "Pie charts are never rendered" in text_of(r)
    assert "kind=bar" in text_of(r)
    assert len(svc.workspace.list_panels(include_closed=True)) == before


def test_every_spec_spelling_is_covered_by_the_guard():
    assert {s.lower() for s in SPELLINGS} == PIE_LIKE_KINDS


def test_mark_and_reason_enums_name_no_pie_form():
    names = [*typing.get_args(Mark), *(r.value for r in PlotReason)]
    assert [n for n in names if FORBIDDEN.search(n) or n in PIE_LIKE_KINDS] == []


def test_only_the_guard_names_a_pie_form_in_source():
    guard = ROOT / "src/telemetry_nerd/core/plot_table.py"
    files = [
        p
        for top in ("src", "ui/src")
        for p in (ROOT / top).rglob("*")
        if p.is_file() and p.suffix in {".py", ".ts", ".svelte", ".js", ".css"} and p != guard
    ]
    assert files
    hits = [str(p.relative_to(ROOT)) for p in files if FORBIDDEN.search(p.read_text())]
    assert hits == [], "pie-like names outside the guard's refusal table"
