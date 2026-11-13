"""plot_data: series supplied by the caller are drawn as a panel with no source behind them."""

from __future__ import annotations

import json

import pytest

from telemetry_nerd.mcp.server import build_mcp

from .fakes import NOW, make_service
from .test_mcp import call, text_of

MINUTE = 60_000
SERIES = [
    {"label": "cats", "ts": [NOW - 2 * MINUTE, NOW - MINUTE, NOW], "values": [1, 4, 2]},
    {"label": "dogs", "ts": [NOW - 2 * MINUTE, NOW], "values": [3, 5]},
]


async def test_supplied_series_become_a_panel_without_touching_the_source(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    r = await call(
        mcp, "plot_data", {"question": "Cats vs dogs?", "series": SERIES, "unit": "pets"}
    )
    assert not r.is_error, text_of(r)
    assert json.loads(text_of(r))["url"] == "http://x/#/panel/p1"
    panel = svc.workspace.get_panel("p1")
    assert panel.question == "Cats vs dogs?"
    assert panel.spec["y"]["unit"] == "pets"
    assert panel.spec["y"]["unit_provenance"] == "provided by claude"
    meta, result = svc.datasets.get(panel.dataset_ids[0])
    assert meta.step_ms == MINUTE and meta.code_node == "inline"
    rows = {(row["series_id"], row["ts_ms"]): row["avg"] for row in result.buckets.to_pylist()}
    assert sorted(rows.values()) == [1, 2, 3, 4, 5]
    assert sorted(json.loads(s["labels"])["series"] for s in result.series.to_pylist()) == [
        "cats",
        "dogs",
    ]


@pytest.mark.parametrize(
    ("series", "complaint"),
    [
        ([], "at least one series"),
        ([{"label": "a", "ts": [NOW], "values": [1, 2]}], "same length"),
        ([{"label": "a", "ts": [NOW, NOW - MINUTE], "values": [1, 2]}], "strictly increasing"),
        ([{"label": "a", "ts": [NOW], "values": [None]}], "finite"),
        ([{"label": "", "ts": [NOW], "values": [1]}], "needs a label"),
        ([{"label": "a", "ts": [NOW, NOW + 1], "values": [1, 2]}], "round them to whole seconds"),
    ],
)
async def test_malformed_series_are_refused_with_the_reason(tmp_path, series, complaint):
    mcp = build_mcp(make_service(tmp_path), "http://x")
    r = await call(mcp, "plot_data", {"question": "q?", "series": series})
    assert r.is_error
    assert complaint in text_of(r)
