"""plot_data: series supplied by the caller are drawn as a panel with no source behind them."""

from __future__ import annotations

import json
from datetime import UTC, datetime

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


CSV = "ts,a,b\n{t0},1,4\n{t1},2,\n{t2},3,6\n"


def csv_text(*stamps: int) -> str:
    return CSV.format(**{f"t{i}": t for i, t in enumerate(stamps)})


async def test_csv_with_an_empty_cell_becomes_a_line_panel_with_one_gap(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://x")
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, UTC).isoformat()
    text = csv_text(iso(NOW - 2 * MINUTE), iso(NOW - MINUTE), iso(NOW))
    r = await call(mcp, "plot_csv", {"question": "a and b?", "csv": text, "unit": "pets"})
    assert not r.is_error, text_of(r)
    panel = svc.workspace.get_panel("p1")
    assert panel.spec["y"]["unit_provenance"] == "provided by claude"
    _, result = svc.datasets.get(panel.dataset_ids[0])
    labels = {s["series_id"]: json.loads(s["labels"])["series"] for s in result.series.to_pylist()}
    points = sorted(
        (labels[b["series_id"]], b["ts_ms"], b["avg"]) for b in result.buckets.to_pylist()
    )
    assert points == [
        ("a", NOW - 2 * MINUTE, 1),
        ("a", NOW - MINUTE, 2),
        ("a", NOW, 3),
        ("b", NOW - 2 * MINUTE, 4),
        ("b", NOW, 6),
    ]


def _lines(count: int) -> list[dict]:
    return [
        {"label": f"s{i}", "ts": [NOW - MINUTE, NOW], "values": [i, i + 1]} for i in range(count)
    ]


@pytest.mark.parametrize(("count", "complaint"), [(6, "small multiples"), (8, "small multiples")])
async def test_six_to_eight_series_are_refused_with_the_small_multiples_hint(
    tmp_path, count, complaint
):
    mcp = build_mcp(make_service(tmp_path), "http://ui")
    r = await call(mcp, "plot_data", {"question": "q?", "series": _lines(count)})
    assert r.is_error
    assert complaint in text_of(r)


async def test_nine_series_are_over_the_limit(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://ui")
    r = await call(mcp, "plot_data", {"question": "q?", "series": _lines(9)})
    assert r.is_error
    assert "limit is 8, received 9" in text_of(r)


async def test_five_series_are_still_drawn(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://ui")
    r = await call(mcp, "plot_data", {"question": "q?", "series": _lines(5)})
    assert not r.is_error


async def test_labelled_columns_with_a_null_become_a_line_panel_with_a_gap(tmp_path):
    service = make_service(tmp_path)
    mcp = build_mcp(service, "http://ui")
    data = {"ts": [NOW - 2 * MINUTE, NOW - MINUTE, NOW], "cats": [1, None, 3], "dogs": [4, 5, 6]}
    r = await call(mcp, "plot_data", {"question": "q?", "data": data})
    assert not r.is_error
    panel = service.workspace.get_panel(json.loads(text_of(r))["panel"])
    _, result = service.datasets.get(panel.dataset_ids[0])
    points = {}
    labels = {s["series_id"]: json.loads(s["labels"])["series"] for s in result.series.to_pylist()}
    for b in result.buckets.to_pylist():
        points.setdefault(labels[b["series_id"]], []).append(b["ts_ms"])
    assert points == {
        "cats": [NOW - 2 * MINUTE, NOW],
        "dogs": [NOW - 2 * MINUTE, NOW - MINUTE, NOW],
    }


async def test_matrix_time_column_does_not_count_against_the_series_limit(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://ui")
    rows = [[NOW - MINUTE, *range(8)], [NOW, *range(8)]]
    r = await call(mcp, "plot_data", {"question": "q?", "data": rows})
    assert r.is_error
    assert "small multiples" in text_of(r)


async def test_matrix_with_named_columns_draws_one_line_per_value_column(tmp_path):
    service = make_service(tmp_path)
    mcp = build_mcp(service, "http://ui")
    rows = [[NOW - MINUTE, 1, 2], [NOW, 3, 4]]
    r = await call(
        mcp, "plot_data", {"question": "q?", "data": rows, "columns": ["when", "a", "b"]}
    )
    assert not r.is_error
    panel = service.workspace.get_panel(json.loads(text_of(r))["panel"])
    _, result = service.datasets.get(panel.dataset_ids[0])
    assert sorted(json.loads(s["labels"])["series"] for s in result.series.to_pylist()) == [
        "a",
        "b",
    ]


async def test_series_and_data_together_are_refused(tmp_path):
    mcp = build_mcp(make_service(tmp_path), "http://ui")
    r = await call(mcp, "plot_data", {"question": "q?", "series": SERIES, "data": {"ts": [1]}})
    assert r.is_error
    assert "exactly one of series or data" in text_of(r)


async def test_plot_data_heatmap_kind_draws_a_matrix_and_refuses_series_and_csv(tmp_path):
    service = make_service(tmp_path)
    mcp = build_mcp(service, "http://ui")
    matrix = [[float(r * 10 + c) for c in range(10)] for r in range(10)]
    r = await call(mcp, "plot_data", {"question": "q?", "kind": "heatmap", "data": matrix})
    assert not r.is_error, text_of(r)
    data = service.panel_data(json.loads(text_of(r))["panel"], width_px=800)
    assert len(data["row_labels"]) == len(data["column_labels"]) == 10

    r = await call(mcp, "plot_data", {"question": "q?", "kind": "heatmap", "series": SERIES})
    assert r.is_error and "takes data" in text_of(r)
    r = await call(mcp, "plot_csv", {"question": "q?", "csv": "a,b\n1,2\n", "kind": "heatmap"})
    assert r.is_error and "not drawable" in text_of(r)
