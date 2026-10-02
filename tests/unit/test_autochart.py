import json

import pytest
from mcp import Client
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.charts.spec import ChartSpec
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.sources.base import SourceError

from .fakes import FakeSource, make_service

NAMES = [
    MetricInfo("app_requests_total", "counter"),
    MetricInfo("app_depth", "gauge"),
    MetricInfo("app_hit_ratio"),
    MetricInfo("app_odd"),
]


class Rec(FakeSource):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.exprs: list[str] = []
        self.fail_on: str | None = None

    async def fetch(self, expr, rng, step_ms):
        self.exprs.append(expr)
        if self.fail_on and self.fail_on in expr:
            raise SourceError("boom")
        return await super().fetch(expr, rng, step_ms)


@pytest.fixture
async def svc(tmp_path):
    src = Rec(name="default", discovery=Discovery(tuple(NAMES), (), {}, None, 1.0, (), False))
    s = make_service(tmp_path, src)
    await s.learn("default")
    s.src = src  # type: ignore[attr-defined]
    return s


async def ds(svc, expr):
    return (await svc.query(expr, start="now-2h", end="now-1h", step="1m"))["dataset"]


def spec_of(svc, res):
    return ChartSpec.model_validate(svc.workspace.get_panel(res.panel.id).spec)


async def test_a_counter_is_charted_as_its_rate_and_the_panel_says_so(svc):
    asked = await ds(svc, "app_requests_total")
    res = await svc.show_auto(asked, "How busy is it?")
    spec = spec_of(svc, res)
    drawn = res.panel.dataset_ids[0]
    assert drawn != asked
    meta = svc.datasets.meta(drawn)
    assert meta.expr.startswith("rate(app_requests_total[") and "$__rate_interval" not in meta.expr
    assert (meta.start_ms, meta.end_ms, meta.step_ms) == (
        svc.datasets.meta(asked).start_ms, svc.datasets.meta(asked).end_ms, svc.datasets.meta(asked).step_ms,
    )  # fmt: skip
    assert spec.auto and spec.auto.transform == "rate" and spec.auto.source_dataset == asked
    assert "counter" in spec.auto.reason and "app_requests_total" in spec.auto.reason
    assert (spec.y.unit, spec.y.unit_provenance) == ("count/s", "inferred from metric name")
    assert svc.datasets.exists(asked)  # the dataset that was asked for is untouched
    assert [i for i in res.issues if i.rule == "raw_counter"] == []


async def test_raw_true_draws_exactly_what_was_asked_without_a_warning(svc):
    asked = await ds(svc, "app_requests_total")
    res = await svc.show_auto(asked, "The running total?", raw=True)
    assert res.panel.dataset_ids == [asked] and spec_of(svc, res).auto is None
    assert [i for i in res.issues if i.rule == "raw_counter"] == []


async def test_gauges_and_unknown_metrics_are_left_alone(svc):
    for expr in ("app_depth", "app_hit_ratio", "app_odd"):
        asked = await ds(svc, expr)
        res = await svc.show_auto(asked, "q?")
        assert res.panel.dataset_ids == [asked] and spec_of(svc, res).auto is None, expr


async def test_a_name_that_says_counter_is_enough_before_the_source_is_learned(tmp_path):
    s = make_service(tmp_path, Rec(name="default"))
    asked = (await s.query("jobs_done_total", start="now-2h", end="now-1h"))["dataset"]
    res = await s.show_auto(asked, "q?")
    assert res.panel.dataset_ids[0] != asked and spec_of(s, res).auto is not None


async def test_the_catalog_decides_a_user_claim_either_way(svc):
    svc.ws.catalog_claim("default", "app_odd", "type", "counter", "user", "user")
    res = await svc.show_auto(await ds(svc, "app_odd"), "q?")
    assert spec_of(svc, res).auto is not None
    svc.ws.catalog_claim("default", "app_requests_total", "type", "gauge", "user", "user")
    asked = await ds(svc, "app_requests_total")
    res = await svc.show_auto(asked, "q?")
    assert res.panel.dataset_ids == [asked]  # declared a gauge by the user: not a counter any more


async def test_expressions_that_cannot_be_rewritten_get_a_warning_not_a_guess(svc):
    asked = await ds(svc, "sum(app_requests_total)")
    res = await svc.show_auto(asked, "q?")
    assert res.panel.dataset_ids == [asked] and spec_of(svc, res).auto is None
    w = [i for i in res.issues if i.rule == "raw_counter"]
    assert len(w) == 1 and "app_requests_total" in w[0].message and "rate(" in w[0].message
    assert not [
        i
        for i in (await svc.show_auto(await ds(svc, "rate(app_requests_total[5m])"), "q?")).issues
        if i.rule == "raw_counter"
    ]


async def test_a_failed_rate_query_falls_back_to_the_raw_chart_with_the_warning(svc):
    asked = await ds(svc, "app_requests_total")
    svc.src.fail_on = "rate("
    res = await svc.show_auto(asked, "q?")
    assert res.panel.dataset_ids == [asked] and spec_of(svc, res).auto is None
    assert any(i.rule == "raw_counter" for i in res.issues)


async def test_other_marks_are_not_rewritten(svc):
    asked = await ds(svc, "app_requests_total")
    with pytest.raises(ValueError):  # a spectrum of a running total is refused as before
        await svc.show_auto(asked, "q?", mark="spectrum")
    assert not any(e.startswith("rate(") for e in svc.src.exprs)


async def test_mcp_show_reports_the_transform(svc):
    asked = await ds(svc, "app_requests_total")
    async with Client(build_mcp(svc, "http://x")) as c:
        out = json.loads(
            (await c.call_tool("show", {"dataset": asked, "question": "q?"})).content[0].text
        )
        assert out["auto"]["transform"] == "rate" and out["auto"]["from"] == asked
        assert out["drawn_dataset"] != asked
        raw = json.loads(
            (await c.call_tool("show", {"dataset": asked, "question": "again?", "raw": True}))
            .content[0]
            .text
        )
        assert "auto" not in raw and "drawn_dataset" not in raw and raw["warnings"] == []
        plain = json.loads(
            (await c.call_tool("show", {"dataset": await ds(svc, "app_depth"), "question": "g?"}))
            .content[0]
            .text
        )
        assert "auto" not in plain


def test_api_show_charts_a_counter_as_rate_and_honours_raw(tmp_path):
    import asyncio

    s = make_service(tmp_path, Rec(name="default"))
    asked = asyncio.run(s.query("jobs_done_total", start="now-2h", end="now-1h"))["dataset"]
    with TestClient(create_app(s, allowed_hosts=["testserver"])) as c:
        p = c.post("/api/show", json={"dataset": asked, "question": "q?"}).json()["panel"]
        assert p["spec"]["auto"]["source_dataset"] == asked and p["dataset_ids"][0] != asked
        r = c.post("/api/show", json={"dataset": asked, "question": "raw?", "raw": True}).json()[
            "panel"
        ]
        assert r["dataset_ids"] == [asked] and r["spec"]["auto"] is None
