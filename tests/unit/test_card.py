import pytest
from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.core import service as service_mod
from telemetry_nerd.core.card_payload import gap_pct
from telemetry_nerd.core.profiles import ProfileRefused
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.sources.base import SourceError

from .fakes import FakeSource, make_service

NAMES = [
    ("node_filesystem_avail_bytes", "gauge", "free space"),
    ("node_filesystem_size_bytes", "gauge", "size"),
    ("app_requests_total", "counter", "requests"),
    ("app_odd", None, None),
]


class Src(FakeSource):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.scrape_calls = 0
        self.scrape_result: int | None = 15_000
        self.scrape_error: str | None = None

    async def scrape_interval(self, selector):
        self.scrape_calls += 1
        if self.scrape_error:
            raise SourceError(self.scrape_error)
        return self.scrape_result


@pytest.fixture
async def svc(tmp_path):
    d = Discovery(
        tuple(MetricInfo(n, t, h, None) for n, t, h in NAMES), (), {}, None, 1.0, (), False
    )
    src = Src(name="default", discovery=d, n_series=2)
    s = make_service(tmp_path, src)
    await s.learn("default")
    s.src = src  # type: ignore[attr-defined]

    async def refuse(source, expr, force=False):
        raise ProfileRefused("no profile in this test")

    s.profiles.ensure = refuse  # type: ignore[method-assign]
    return s


async def panel(svc, expr):
    ds = (await svc.query(expr, start="now-2h", end="now-1h"))["dataset"]
    return svc.show(ds, "q?").panel.id


def field(card, metric, name):
    m = next(m for m in card["metrics"] if m["metric"] == metric)
    return next(f for f in m["fields"] if f["field"] == name)


async def test_card_lists_claims_with_provenance(svc):
    card = await svc.panel_card(await panel(svc, "node_filesystem_avail_bytes"))
    assert card["learned"] and [m["metric"] for m in card["metrics"]] == [
        "node_filesystem_avail_bytes"
    ]
    unit = field(card, "node_filesystem_avail_bytes", "unit")
    assert (unit["value"], unit["origin"], unit["editable"]) == ("B", "pack", True)
    assert {c["origin"] for c in unit["claims"]} >= {"pack", "rule"}
    assert unit["basis"].startswith("pack node_exporter@")
    role = field(card, "node_filesystem_avail_bytes", "role")
    assert role["value"] == "capacity"


async def test_unclaimed_core_fields_are_still_rows_so_they_can_be_filled(svc):
    card = await svc.panel_card(await panel(svc, "app_odd"))
    rows = {f["field"]: f for f in card["metrics"][0]["fields"]}
    assert (
        rows["role"]["value"] is None and rows["role"]["editable"] and rows["role"]["claims"] == []
    )
    assert "histogram_family" not in rows  # not editable, nothing claimed


async def test_conflicts_are_flagged(svc):
    svc.ws.catalog_claim(
        "default", "app_odd", "unit", "s", "claude", "claude", confidence=0.7, citation="b"
    )
    svc.ws.catalog_claim(
        "default", "app_odd", "unit", "ms", "pack", "system", confidence=0.85
    ) if False else None
    svc.ws.catalog_claim("default", "app_odd", "unit", "ms", "metadata", "system", confidence=0.9)
    card = await svc.panel_card(await panel(svc, "app_odd"))
    unit = field(card, "app_odd", "unit")
    assert unit["conflict"] is True and unit["value"] == "s" and unit["origin"] == "claude"


async def test_relations_bindings_and_gaps_are_linked(svc):
    ws = svc.ws
    ws.bind(
        "default",
        "RED",
        "api",
        {"rate": "app_requests_total", "errors": None, "duration": None},
        "user",
        "user",
    )
    card = await svc.panel_card(await panel(svc, "app_requests_total"))
    m = card["metrics"][0]
    assert m["bindings"][0]["key"] == "api" and m["bindings"][0]["roles"]["errors"] is None
    assert {g["role"] for g in m["gaps"]} == {"errors", "duration"}
    avail = await svc.panel_card(await panel(svc, "node_filesystem_avail_bytes"))
    assert avail["metrics"][0]["relations"][0]["object"] == "node_filesystem_size_bytes"


async def test_up_to_three_metrics_and_plain_selector_first(svc):
    card = await svc.panel_card(
        await panel(
            svc,
            "app_requests_total + node_filesystem_avail_bytes + node_filesystem_size_bytes + app_odd",
        )
    )
    assert len(card["metrics"]) == 3


async def test_an_unlearned_source_says_so(tmp_path):
    s = make_service(tmp_path)
    ds = (await s.query("up", start="now-2h", end="now-1h"))["dataset"]
    pid = s.show(ds, "q?").panel.id
    card = await s.panel_card(pid)
    assert card["learned"] is False and card["metrics"] == []


async def test_profile_refused_and_present(svc):
    card = await svc.panel_card(await panel(svc, "app_odd"))
    assert card["profile"]["available"] is False  # not computed in the background in tests either
    prof = svc.profiles
    from types import SimpleNamespace

    from telemetry_nerd.analysis.profile import RangeStats

    fake = SimpleNamespace(
        window_ms=30 * 86_400_000, step_ms=3_600_000, kind="level", stale=True, computed_at_ms=1,
        series_total=2, pooled=RangeStats(n=5, p005=1.0, p995=9.0), caveats=["x"], series=[],
    )  # fmt: skip
    prof.cached = lambda source, expr: fake  # type: ignore[method-assign]
    card = await svc.panel_card(await panel(svc, "app_odd"))
    assert card["profile"]["available"] and card["profile"]["stale"] is True
    assert card["profile"]["range"]["p995"] == 9.0


async def test_quality_numbers_and_honest_unknowns(svc):
    card = await svc.panel_card(await panel(svc, "app_odd"))
    q = card["quality"]
    assert q["scrape_interval_ms"] == 15_000 and q["series"] == 2
    assert q["gap_pct"] == 0.0
    assert q["resets"]["measured"] is False and q["cardinality"] == {"in_panel": 2, "catalog": None}


async def test_scrape_interval_is_cached_for_an_hour_and_failures_are_reasons(svc, monkeypatch):
    pid = await panel(svc, "app_odd")
    await svc.panel_card(pid)
    await svc.panel_card(pid)
    assert svc.src.scrape_calls == 1
    svc.src.scrape_error = "boom"
    t = [svc.clock() + service_mod.SCRAPE_CACHE_MS + 1]
    monkeypatch.setattr(svc, "clock", lambda: t[0])
    q = (await svc.panel_card(pid))["quality"]
    assert q["scrape_interval_ms"] is None and "boom" in q["scrape_interval_reason"]
    assert svc.src.scrape_calls == 2


async def test_expression_that_is_not_a_single_metric_has_no_scrape_interval(svc):
    q = (await svc.panel_card(await panel(svc, "app_odd * 2")))["quality"]
    assert q["scrape_interval_ms"] == 15_000  # one catalogued metric: that one is measured
    nothing = (await svc.panel_card(await panel(svc, "vector(1)")))["quality"]
    assert (
        nothing["scrape_interval_ms"] is None
        and "not a single metric" in nothing["scrape_interval_reason"]
    )


def test_gap_pct_counts_missing_buckets():
    import pyarrow as pa

    from telemetry_nerd.model.series import BUCKET_SCHEMA

    t = pa.table(
        {
            "ts_ms": [0, 60_000, 0, 120_000],
            "series_id": ["a", "a", "b", "b"],
            "avg": [1.0, None, 2.0, float("nan")],
            "min": [1.0, None, 2.0, 1.0],
            "max": [1.0, None, 2.0, 1.0],
            "count": [1, None, 1, 1],
        },
        schema=BUCKET_SCHEMA,
    )
    # 3 expected buckets x 2 series = 6; 2 usable values
    assert gap_pct(t, 0, 120_000, 60_000) == round(1 - 2 / 6, 4)


def test_claim_route_confirms_edits_and_rejects(tmp_path):
    import asyncio

    d = Discovery((MetricInfo("app_odd"),), (), {}, None, 1.0, (), False)
    s = make_service(tmp_path, FakeSource(name="default", discovery=d))
    asyncio.run(s.learn("default"))
    with TestClient(create_app(s, allowed_hosts=["testserver"])) as c:
        ok = c.post(
            "/api/catalog/claims",
            json={"source": "default", "metric": "app_odd", "field": "unit", "value": "ms"},
        )
        assert (
            ok.status_code == 200
            and ok.json()["origin"] == "user"
            and ok.json()["confidence"] == 1.0
        )
        assert s.ws.catalog_entry("default", "app_odd").fields["unit"].value == "ms"
        ev = [e for e in s.ws.log.since(0) if e.type == "catalog.claimed"][-1]
        assert (ev.actor, ev.klass) == ("user", "intentional")
        bad = c.post(
            "/api/catalog/claims",
            json={"source": "default", "metric": "app_odd", "field": "bounds", "value": "positive"},
        )
        assert bad.status_code == 400
        assert (
            c.post(
                "/api/catalog/claims",
                json={"source": "default", "metric": "ghost", "field": "unit", "value": "s"},
            ).status_code
            == 404
        )
        assert (
            c.post(
                "/api/catalog/claims",
                json={"source": "default", "metric": "app_odd", "field": "unit"},
            ).status_code
            == 400
        )
        assert (
            c.post(
                "/api/catalog/claims",
                json={"source": "default", "metric": "app_odd", "field": "nope", "value": "x"},
            ).status_code
            == 400
        )
        card = c.get("/api/panels/p99/card")
        assert card.status_code == 404


async def test_a_unit_claim_updates_the_axis_of_open_panels_that_use_the_metric(svc):
    pid = await panel(svc, "app_odd")
    before = svc.workspace.get_panel(pid).spec["y"]
    assert before["unit"] is None
    other = await panel(svc, "app_requests_total")  # unrelated metric: untouched
    svc.ws.catalog_claim("default", "app_odd", "unit", "ms", "user", "user")
    after = svc.workspace.get_panel(pid).spec["y"]
    assert (after["unit"], after["unit_provenance"]) == ("ms", "set by user")
    assert svc.workspace.get_panel(other).spec["y"]["unit"] == "count"
    ev = [e for e in svc.ws.log.since(0) if e.type == "panel.unit_refreshed"]
    assert ev and ev[-1].object_id == pid and ev[-1].payload["unit"] == "ms"


async def test_a_unit_stated_when_the_panel_was_shown_is_not_overridden(svc):
    ds = (await svc.query("app_odd", start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "q?", unit="items").panel.id
    svc.ws.catalog_claim("default", "app_odd", "unit", "ms", "user", "user")
    y = svc.workspace.get_panel(pid).spec["y"]
    assert (y["unit"], y["unit_provenance"]) == ("items", "provided by claude")


async def test_closed_panels_and_other_fields_are_left_alone(svc):
    pid = await panel(svc, "app_odd")
    svc.ws.catalog_claim("default", "app_odd", "role", "state", "user", "user")
    assert [e for e in svc.ws.log.since(0) if e.type == "panel.unit_refreshed"] == []
    svc.ws.close_panel(pid, "user")
    svc.ws.catalog_claim("default", "app_odd", "unit", "ms", "user", "user")
    assert svc.workspace.get_panel(pid).spec["y"]["unit"] is None


async def test_a_type_claim_changes_the_rate_unit(svc):
    pid = await panel(svc, "rate(app_odd[5m])")
    assert svc.workspace.get_panel(pid).spec["y"]["unit"] is None
    svc.ws.catalog_claim("default", "app_odd", "unit", "s", "user", "user")
    assert svc.workspace.get_panel(pid).spec["y"]["unit"] == "s"
    svc.ws.catalog_claim("default", "app_odd", "type", "counter", "user", "user")
    assert svc.workspace.get_panel(pid).spec["y"]["unit"] == "s/s"
