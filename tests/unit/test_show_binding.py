"""show_binding (bead czt.3): a USE / RED / Little's law binding as one linked panel group."""

import json
import math

import numpy as np
import pyarrow as pa
import pytest
from mcp import Client

from telemetry_nerd.analysis.fraction import wilson
from telemetry_nerd.core.binding_view import (
    Hint,
    MetricInfo,
    error_ratio,
    hint_expr,
    plan_role,
    with_matchers,
)
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.model.discovery import MetricInfo as DiscoveredMetric
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceError

from .fakes import FakeSource, make_service

RI = "$__rate_interval"

# --- planning (pure) -----------------------------------------------------------------------------
REQ = MetricInfo("http_requests_total", "counter")
HIST = MetricInfo("http_request_duration_seconds", "histogram")


def test_rate_of_a_counter_is_summed_by_the_join_labels():
    p = plan_role("RED", "rate", REQ, key="cart", join_on=["service"])
    assert p.form == "rate"
    assert p.expr == f"sum by (service) (rate(http_requests_total[{RI}]))"


def test_rate_from_a_histogram_counts_its_observations_classic_and_native():
    p = plan_role("littles_law", "arrival_rate", HIST, key="x")
    assert p.expr == f"sum (rate(http_request_duration_seconds_count[{RI}]))"
    nat = MetricInfo("lat", "histogram", native=True)
    assert "histogram_count(rate(lat[" in plan_role("RED", "rate", nat, key="x").expr


def test_errors_split_by_the_status_matcher_of_the_hint_over_the_same_metric():
    hint = Hint("label_split", 'sum by (service) (rate(http_requests_total{code=~"5.."}[5m]))')
    p = plan_role(
        "RED", "errors", REQ, key="cart", join_on=["service"], hint=hint, rate_metric=REQ,
        matchers={"env": "prod"},
    )  # fmt: skip
    assert p.form == "error_ratio" and p.unit == "ratio" and p.bounds == (0.0, 1.0)
    assert p.num == f'sum by (service) (rate(http_requests_total{{env="prod",code=~"5.."}}[{RI}]))'
    assert p.den == f'sum by (service) (rate(http_requests_total{{env="prod"}}[{RI}]))'


def test_a_dedicated_error_counter_is_divided_by_the_rate_roles_requests():
    err = MetricInfo("http_request_errors_total", "counter")
    p = plan_role("RED", "errors", err, key="x", join_on=["service"], rate_metric=HIST)
    assert p.form == "error_ratio"
    assert p.num == f"sum by (service) (rate(http_request_errors_total[{RI}]))"
    assert p.den == f"sum by (service) (rate(http_request_duration_seconds_count[{RI}]))"


def test_an_error_split_with_no_known_status_label_is_unresolved_not_guessed():
    hint = Hint("label_split", 'sum(rate(x_total{<status-label>=~"5.."}[5m]))')
    p = plan_role("RED", "errors", REQ, key="x", hint=hint, rate_metric=REQ)
    assert p.unresolved and "error_matcher" in p.unresolved
    ok = plan_role(
        "RED", "errors", REQ, key="x", hint=hint, rate_metric=REQ, error_matcher='status="500"'
    )
    assert ok.unresolved is None and 'status="500"' in ok.num


def test_errors_without_a_rate_role_are_a_rate_and_say_so():
    err = MetricInfo("x_errors_total", "counter")
    p = plan_role("RED", "errors", err, key="x")
    assert p.form == "errors" and any("not a share" in n for n in p.notes)


def test_latency_is_the_distribution_never_a_percentile():
    p = plan_role("RED", "duration", HIST, key="x", join_on=["service"], matchers={"env": "a"})
    assert p.form == "distribution"
    assert p.selector == 'http_request_duration_seconds_bucket{env="a"}' and p.by == ("service",)
    summ = plan_role("RED", "duration", MetricInfo("rpc_latency", "summary"), key="x")
    assert summ.form == "mean" and "_sum" in summ.expr and "quantile" not in summ.expr
    assert any("cannot be merged" in n for n in summ.notes)


def test_bounded_saturation_stays_per_series_for_the_limit_line():
    m = MetricInfo("queue_depth", "gauge", bounded=True)
    p = plan_role(
        "USE",
        "saturation",
        m,
        key="q",
        join_on=["instance"],
        hint=Hint("gauge", "sum by (instance) (queue_depth)"),
    )
    assert p.expr == "queue_depth" and any("bounded_by" in n for n in p.notes)


def test_utilization_uses_the_hint_expr_with_the_step_aware_window_and_matchers():
    m = MetricInfo("node_cpu_seconds_total", "counter")
    hint = Hint("ratio", '1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))')
    p = plan_role(
        "USE", "utilization", m, key="node", hint=hint, matchers={"instance": "a:9100"},
        names=["node_cpu_seconds_total"],
    )  # fmt: skip
    assert p.expr == (
        '1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",instance="a:9100"}'
        f"[{RI}]))"
    )


def test_hint_comments_and_histogram_hints_are_not_queries():
    assert hint_expr(Hint("histogram", "rate(x_sum[5m]) / rate(x_count[5m])  # mean W")) is None
    assert hint_expr(Hint("gauge", "sum(x)  # note")) == "sum(x)"


def test_with_matchers_matches_whole_names_only():
    out = with_matchers("a / a_total + rate(a{b='c'}[1m])", ["a"], {"x": "1"})
    assert out == 'a{x="1"} / a_total + rate(a{b=\'c\',x="1"}[1m])'
    with pytest.raises(ValueError, match="label name"):
        with_matchers("a", ["a"], {"bad-label": "1"})


def _result(rows, source="s"):
    """rows: (labels, ts, avg)"""
    labs = {json.dumps(lb, sort_keys=True): lb for lb, _, _ in rows}
    sid = {k: series_id(source, lb) for k, lb in labs.items()}
    t = pa.table(
        {
            "ts_ms": [r[1] for r in rows],
            "series_id": [sid[json.dumps(r[0], sort_keys=True)] for r in rows],
            "avg": [r[2] for r in rows],
            "min": [r[2] for r in rows],
            "max": [r[2] for r in rows],
            "count": [4] * len(rows),
        },
        schema=BUCKET_SCHEMA,
    )
    s = pa.table(
        {"series_id": list(sid.values()), "labels": [labels_json(lb) for lb in labs.values()]},
        schema=SERIES_SCHEMA,
    )
    return FetchResult(t, s)


def test_error_ratio_is_counts_over_requests_with_a_wilson_band():
    a, b = {"service": "a"}, {"service": "b"}
    num = _result([(a, 60_000, 0.5), (a, 120_000, 0.0)])
    den = _result([(a, 60_000, 10.0), (a, 120_000, 10.0), (b, 60_000, 5.0), (b, 120_000, 0.0)])
    out, notes = error_ratio(num, den, 60_000)
    rows = {(r["series_id"], r["ts_ms"]): r for r in out.buckets.to_pylist()}
    ra = rows[(series_id("s", a), 60_000)]
    assert ra["avg"] == pytest.approx(0.05)
    lo, hi = wilson(30.0, 600.0)  # 0.5/s and 10/s over a 60 s step
    assert (ra["lo"], ra["hi"]) == (pytest.approx(lo), pytest.approx(hi))
    # b has no error series: 0 errors, said so; a step with no requests has no share at all
    assert rows[(series_id("s", b), 60_000)]["avg"] == 0.0
    assert (series_id("s", b), 120_000) not in rows
    assert any("no error series" in n for n in notes)
    assert out.buckets.column_names[-2:] == ["lo", "hi"]
    assert all(0 <= r["lo"] <= r["avg"] <= r["hi"] <= 1 for r in rows.values())


def test_more_errors_than_requests_is_clipped_and_noted():
    a = {"service": "a"}
    out, notes = error_ratio(_result([(a, 1, 3.0)]), _result([(a, 1, 2.0)]), 60_000)
    assert out.buckets.to_pylist()[0]["avg"] == 1.0 and any("clipped" in n for n in notes)


# --- through the service -------------------------------------------------------------------------
METRICS = (
    ("http_server_requests_total", "counter"),
    ("http_server_request_duration_seconds", "histogram"),
    ("http_server_active_requests", "gauge"),
    ("node_cpu_seconds_total", "counter"),
    ("node_pressure_cpu_waiting_seconds_total", "counter"),
)


class BindingSource(FakeSource):
    """Per-service series whose values depend on what the expression asks for."""

    def __init__(self, members=2):
        ms = tuple(DiscoveredMetric(n, t, None, None) for n, t in METRICS)
        hist = {n: "classic" for n, t in METRICS if t == "histogram"}
        super().__init__(
            name="default", n_series=2,
            discovery=Discovery(ms, ("service_name", "instance"), hist, None, 1.0, (), False),
        )  # fmt: skip
        self.members = members
        self.exprs: list[str] = []
        self.fail_on: str | None = None

    async def fetch_histogram(self, selector, by, rng, step_ms):
        if by:
            return await super().fetch_histogram(selector, by, rng, step_ms)
        n, self.n_series = self.n_series, 1  # merged: one distribution
        try:
            return await super().fetch_histogram(selector, by, rng, step_ms)
        finally:
            self.n_series = n

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        self.exprs.append(expr)
        if self.fail_on and self.fail_on in expr:
            raise SourceError("boom", hint="test")
        label = "instance" if "node_" in expr else "service_name"
        names = [f"s{k}" for k in range(self.members)]
        if "5.." in expr:
            vals = {names[0]: 2.0}  # only the first member has had errors
        elif "active_requests" in expr:
            vals = dict.fromkeys(names, 7.0)
        elif "idle" in expr:
            vals = dict.fromkeys(names, 0.4)
        else:
            vals = dict.fromkeys(names, 100.0)
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        labels = {n: {label: n} for n in vals}
        sids = {n: series_id(self.name, lb) for n, lb in labels.items()}
        rows = [(t, sids[n], v) for n, v in vals.items() for t in ts]
        buckets = pa.table(
            {
                "ts_ms": [r[0] for r in rows],
                "series_id": [r[1] for r in rows],
                "avg": [r[2] for r in rows],
                "min": [r[2] for r in rows],
                "max": [r[2] for r in rows],
                "count": [4] * len(rows),
            },
            schema=BUCKET_SCHEMA,
        )
        series = pa.table(
            {
                "series_id": list(sids.values()),
                "labels": [labels_json(lb) for lb in labels.values()],
            },
            schema=SERIES_SCHEMA,
        )
        return FetchResult(buckets, series)


async def _svc(tmp_path, members=2):
    src = BindingSource(members)
    svc = make_service(tmp_path, src)
    await svc.learn("default")
    return svc, src


async def test_red_suggestion_draws_three_linked_panels_in_one_group(tmp_path):
    svc, src = await _svc(tmp_path)
    g = await svc.show_binding(source="default", suggestion="RED:otel_http", start="now-2h")
    assert (g.kind, g.key, g.basis) == ("RED", "http.server", "suggestion")
    roles = {r.role: r for r in g.roles}
    assert list(roles) == ["rate", "errors", "duration"]
    assert all(r.panel for r in roles.values()), [r.error for r in roles.values()]
    assert roles["rate"].view == "lines" and roles["rate"].form == "rate"
    assert roles["errors"].form == "error_ratio" and roles["duration"].view == "heatmap"
    panels = {r: svc.workspace.get_panel(roles[r].panel) for r in roles}
    assert {p.spec["group"]["id"] for p in panels.values()} == {g.id}
    assert {p.spec["group"]["role"] for p in panels.values()} == set(roles)
    # one window and step for every role, so the x axes line up
    metas = [svc.datasets.meta(p.dataset_ids[0]) for p in panels.values()]
    assert {(m.start_ms, m.end_ms, m.step_ms) for m in metas} == {(g.start_ms, g.end_ms, g.step_ms)}
    # errors: a share with a Wilson band, on its natural axis
    em = svc.datasets.meta(panels["errors"].dataset_ids[0])
    assert em.uncertainty["level"] == 0.95 and "Wilson" in em.uncertainty["method"]
    assert em.producer["kind"] == "binding" and len(em.parents) == 2
    assert panels["errors"].spec["y"]["unit"] == "ratio"
    assert panels["errors"].spec["y"]["asserted_bounds"] == {
        "lo": 0.0, "hi": 1.0, "by": "the RED errors role (a share of requests)",
    }  # fmt: skip
    iv = svc.datasets.interval(em.id).to_pylist()
    assert iv and all(0 <= r["lo"] <= r["hi"] <= 1 for r in iv)
    assert any('http_response_status_code=~"5.."' in e for e in src.exprs)
    # latency: the histogram's distribution, never a percentile
    assert src.hist_selectors == ["http_server_request_duration_seconds_bucket"]  # 2 members: kept
    assert panels["duration"].spec["layers"][0]["mark"] == "heatmap"
    snap = svc.ws.snapshot()
    assert [x["id"] for x in snap["groups"]] == [g.id]


async def test_a_confirmed_binding_with_a_null_role_shows_a_gap_card(tmp_path):
    svc, _ = await _svc(tmp_path)
    svc.ws.binding_accept("default", "USE:node_cpu", basis="test", overrides={"saturation": None})
    g = await svc.show_binding(source="default", kind="USE", key="node:cpu")
    roles = {r.role: r for r in g.roles}
    assert g.basis == "binding" and g.binding_origin == "claude"
    assert roles["utilization"].panel and roles["utilization"].form == "utilization"
    for role in ("saturation", "errors"):
        gap = roles[role]
        assert gap.view == "gap" and gap.panel is None and gap.suggestion is not None
        assert gap.gap and gap.gap.startswith("g")  # the Gap the binding raised
    assert roles["saturation"].suggestion.type == "gauge"
    p = svc.workspace.get_panel(roles["utilization"].panel)
    ab = p.spec["y"].get("asserted_bounds")
    ctx = p.spec["y"]["context"]
    # a share of capacity: 0..1, from a derivation rule or from the role itself
    assert (ctx["natural_lo"], ctx["natural_hi"]) == (0.0, 1.0) or (ab and ab["hi"] == 1.0)


async def test_no_binding_is_an_error_that_points_at_binding_suggest(tmp_path):
    svc, _ = await _svc(tmp_path)
    with pytest.raises(NotFound, match="binding_suggest"):
        await svc.show_binding(source="default", kind="RED", key="nope")
    with pytest.raises(ValueError, match="kind and key"):
        await svc.show_binding(source="default")


async def test_many_members_become_fleets_and_many_distributions_merge(tmp_path):
    svc, src = await _svc(tmp_path, members=8)
    src.n_series = 6  # histogram members
    g = await svc.show_binding(source="default", suggestion="littles_law:otel_http")
    roles = {r.role: r for r in g.roles}
    assert roles["arrival_rate"].view == "fleet" and roles["arrival_rate"].members == 8
    assert roles["concurrency"].view == "fleet"
    p = svc.workspace.get_panel(roles["concurrency"].panel)
    assert p.spec["layers"][0]["mark"] == "fleet"
    lat = roles["latency"]
    assert lat.view == "heatmap" and lat.members == 6
    assert any("merged" in n for n in lat.notes)
    assert svc.datasets.series_count(svc.workspace.get_panel(lat.panel).dataset_ids[0]) == 1


async def test_closing_the_group_closes_every_panel(tmp_path):
    svc, _ = await _svc(tmp_path)
    g = await svc.show_binding(source="default", suggestion="RED:otel_http")
    svc.ws.close_group(g.id, "user")
    assert all(svc.workspace.get_panel(r.panel).closed for r in g.roles if r.panel)
    assert svc.ws.snapshot()["groups"] == []


async def test_reframing_the_group_makes_a_new_group_over_the_selection(tmp_path):
    svc, _ = await _svc(tmp_path)
    g = await svc.show_binding(source="default", suggestion="RED:otel_http", start="now-6h")
    a, b = g.start_ms + 3_600_000, g.start_ms + 7_200_000
    g2 = await svc.reframe_group(g.id, a, b)
    assert g2.id != g.id and g2.reframed_from == g.id and (g2.start_ms, g2.end_ms) == (a, b)
    assert g2.step_ms < g.step_ms
    assert not svc.workspace.get_panel(g.roles[0].panel).closed  # the original stays


async def test_a_confirmed_binding_takes_its_error_split_from_the_suggestions_hint(tmp_path):
    svc, _ = await _svc(tmp_path)
    svc.ws.bind_claude(
        "default", "RED", "svc", {
            "rate": "http_server_requests_total", "errors": "http_server_requests_total",
            "duration": "http_server_request_duration_seconds",
        }, join_on=["service_name"], basis="test", confidence=0.8,
    )  # fmt: skip
    g = await svc.show_binding(source="default", kind="RED", key="svc")
    assert {r.role: r.form for r in g.roles}["errors"] == "error_ratio"
    assert all(r.panel for r in g.roles)


async def test_a_role_that_cannot_be_fetched_is_reported_not_fatal(tmp_path):
    svc, src = await _svc(tmp_path)
    src.fail_on = "active_requests"
    g = await svc.show_binding(source="default", suggestion="littles_law:otel_http")
    roles = {r.role: r for r in g.roles}
    assert roles["concurrency"].view == "error" and "boom" in roles["concurrency"].error
    assert roles["arrival_rate"].panel and roles["latency"].panel


async def test_mcp_show_binding_returns_panel_ids_per_role_and_gaps(tmp_path):
    svc, _ = await _svc(tmp_path)
    mcp = build_mcp(svc, "http://ui")
    async with Client(mcp) as c:
        res = await c.call_tool(
            "show_binding", {"suggestion": "littles_law:otel_http", "range": "3h"}
        )
    out = json.loads(res.content[0].text)
    assert out["kind"] == "littles_law" and out["url"].startswith("http://ui/#/group/")
    # the three signals, plus czt.2's L vs λ·W model panel over the same window
    assert set(out["roles"]) == {"arrival_rate", "latency", "concurrency", "check"}
    assert out["roles"]["check"]["view"] == "model" and out["roles"]["check"]["form"] == "littles"
    assert all(r["panel"].startswith("p") for r in out["roles"].values())
    assert out["gaps"] == []
    span = TimeRange(svc.ws.group_get(out["group"]).start_ms, svc.ws.group_get(out["group"]).end_ms)
    assert math.isclose(span.end_ms - span.start_ms, 3 * 3_600_000)


def test_http_group_close_and_reframe(tmp_path):
    from functools import partial

    from starlette.testclient import TestClient

    from telemetry_nerd.api.app import create_app

    svc = make_service(tmp_path, BindingSource())
    with TestClient(create_app(svc, allowed_hosts=["testserver"])) as c:
        c.portal.call(svc.learn, "default")
        g = c.portal.call(
            partial(svc.show_binding, source="default", suggestion="RED:otel_http", start="now-6h")
        )
        bad = c.post(f"/api/groups/{g.id}/reframe", json={"start_ms": 5, "end_ms": 1})
        assert bad.status_code == 400
        a, b = g.start_ms + 3_600_000, g.start_ms + 7_200_000
        r = c.post(f"/api/groups/{g.id}/reframe", json={"start_ms": a, "end_ms": b})
        assert r.status_code == 200 and r.json()["reframed_from"] == g.id
        assert c.post(f"/api/groups/{g.id}/close", json={}).status_code == 200
        assert c.post("/api/groups/pg99/close", json={}).status_code == 404
        snap = c.get("/api/workspace").json()
        events = c.get("/api/events?since=0").json()["events"]
    assert [x["id"] for x in snap["groups"]] == [r.json()["id"]]
    closed = [e for e in events if e["type"] == "panel_group.closed"]
    assert [e["object_id"] for e in closed] == [g.id]


async def test_littles_group_adds_the_check_panel_drawn_with_its_own_mark(tmp_path):
    svc, _ = await _svc(tmp_path)
    g = await svc.show_binding(source="default", suggestion="littles_law:otel_http", start="now-3h")
    check = g.roles[-1]
    assert check.role == "check" and check.view == "model" and check.panel
    p = svc.workspace.get_panel(check.panel)
    assert p.spec["layers"][0]["mark"] == "littles"
    assert p.spec["group"] == {"id": g.id, "role": "check"}
    assert any(n.startswith("verdict: ") for n in check.notes)


async def test_littles_check_refusal_is_a_card_not_a_failure(tmp_path):
    svc, _ = await _svc(tmp_path)
    svc.ws.bind_claude(
        "default", "littles_law", "svc", {
            "arrival_rate": "http_server_requests_total",
            "latency": "http_server_request_duration_seconds",
            "concurrency": "http_server_requests_total",  # a counter: czt.2 refuses it as L
        }, join_on=["service_name"], basis="test", confidence=0.8,
    )  # fmt: skip
    g = await svc.show_binding(source="default", kind="littles_law", key="svc", start="now-3h")
    check = {r.role: r for r in g.roles}["check"]
    assert check.view == "error" and "counter" in check.error


def test_a_bound_bucket_series_stands_for_its_classic_histogram(tmp_path):
    from telemetry_nerd.core.binding_ops import BindingOps

    svc = make_service(tmp_path, BindingSource())
    names = ["lat_seconds_bucket", "lat_seconds_count", "lat_seconds_sum"]
    svc.ws.catalog.relearn("default", names, 1, complete=True)
    info = BindingOps(svc)._info("default", "lat_seconds_bucket", frozenset())
    assert (info.name, info.type, info.native) == ("lat_seconds", "histogram", False)


async def test_littles_check_without_concurrency_says_it_cannot_be_done(tmp_path):
    """60j: no in-flight gauge, no check: the group says so plainly, with the gauge to add, and
    never derives L from lambda W."""
    svc, _ = await _svc(tmp_path)
    svc.ws.bind_claude(
        "default", "littles_law", "svc", {
            "arrival_rate": "http_server_requests_total",
            "latency": "http_server_request_duration_seconds", "concurrency": None,
        }, join_on=["service_name"], basis="test", confidence=0.8,
    )  # fmt: skip
    g = await svc.show_binding(source="default", kind="littles_law", key="svc", start="now-3h")
    roles = {r.role: r for r in g.roles}
    assert roles["concurrency"].view == "gap"
    check = roles["check"]
    assert check.view == "gap" and check.panel is None and check.suggestion
    assert "cannot be checked without a concurrency" in check.why
    out = await svc.binding_verdict(
        source="default", kind="littles_law", key="svc", start="now-1h", reference="previous"
    )
    mc = out["roles"]["concurrency"]["model_check"]
    assert mc["status"] == "not_possible" and "L was not estimated" in mc["summary"]


# --- verdict ratio counts: absent error series is 0 (disclosed); unknown steps stay unknown ------
def _ratio(num_rows, den_rows, grid, shift=0):
    from types import SimpleNamespace

    import numpy as np

    from telemetry_nerd.core.verdict_ops import VerdictOps

    parents = SimpleNamespace(parents=["num", "den"])
    data = {"num": _result(num_rows) if num_rows is not None else None, "den": _result(den_rows)}
    datasets = SimpleNamespace(meta=lambda ds: parents, get=lambda ds: (None, data[ds]))
    ops = VerdictOps(SimpleNamespace(datasets=datasets))  # type: ignore[arg-type]
    stats = ops._ratio_stats()
    a, n = ops._ratio_counts("r", np.array(grid), shift, 60.0, stats)
    notes: list[str] = []
    ops._ratio_notes(stats, notes)
    return a, n, notes


def test_verdict_absent_error_series_reads_as_zero_and_says_so():
    den = [({"service": "a"}, 60_000, 10.0), ({"service": "a"}, 120_000, 10.0)]
    a, n, notes = _ratio([], den, [60_000, 120_000, 180_000])
    assert list(a[:2]) == [0.0, 0.0] and list(n[:2]) == [600.0, 600.0]
    assert math.isnan(a[2]) and math.isnan(n[2])  # a step without requests has no share
    assert any("no error series" in x for x in notes)


def test_verdict_error_step_without_a_value_stays_unknown_not_zero():
    a_ = {"service": "a"}
    num = [(a_, 60_000, 0.5), (a_, 120_000, float("nan"))]
    den = [(a_, 60_000, 10.0), (a_, 120_000, 10.0)]
    a, n, notes = _ratio(num, den, [60_000, 120_000])
    assert a[0] == pytest.approx(30.0) and math.isnan(a[1])
    assert math.isnan(n[1])  # excluded from both sides
    assert any("no value at 1 member-steps: excluded" in x for x in notes)


def test_error_ratio_step_gap_in_an_existing_error_series_is_excluded_and_disclosed():
    a = {"service": "a"}
    num = _result([(a, 60_000, 0.5)])
    den = _result([(a, 60_000, 10.0), (a, 120_000, 10.0)])
    out, notes = error_ratio(num, den, 60_000)
    assert 120_000 not in {r["ts_ms"] for r in out.buckets.to_pylist()}  # excluded, not 0
    assert any("no value at 1 member-steps: excluded" in x for x in notes)


def test_verdict_members_without_an_error_series_are_named_and_gaps_excluded():
    a_, b_ = {"service": "a"}, {"service": "b"}
    num = [(a_, 60_000, 0.5), (a_, 120_000, float("nan"))]
    den = [(a_, 60_000, 10.0), (a_, 120_000, 10.0), (b_, 60_000, 5.0), (b_, 120_000, 5.0)]
    a, n, notes = _ratio(num, den, [60_000, 120_000])
    assert a[0] == pytest.approx(30.0) and n[0] == pytest.approx(900.0)
    assert n[1] == pytest.approx(300.0) and a[1] == 0.0  # a's gap step is out of both sides
    assert any("without an error series counted as 0 errors" in x and "service" in x for x in notes)
    assert any("no value at 1 member-steps: excluded" in x for x in notes)


def test_verdict_all_nan_error_series_is_a_gap_not_an_absent_series():
    import numpy as np

    a_ = {"service": "a"}
    num = [(a_, 60_000, float("nan")), (a_, 120_000, float("nan"))]
    den = [(a_, 60_000, 10.0), (a_, 120_000, 10.0)]
    a, n, notes = _ratio(num, den, [60_000, 120_000])
    assert np.isnan(a).all() and np.isnan(n).all()
    assert not any("counted as 0" in x and "series" in x and "excluded" not in x for x in notes)
    assert any("no value at 2 member-steps: excluded" in x for x in notes)


def test_verdict_all_nan_error_series_in_a_fleet_is_not_named_absent():
    a_, b_ = {"service": "a"}, {"service": "b"}
    num = [(a_, 60_000, 0.5), (b_, 60_000, float("nan"))]
    den = [(a_, 60_000, 10.0), (b_, 60_000, 5.0)]
    _, n, notes = _ratio(num, den, [60_000])
    assert n[0] == pytest.approx(600.0)  # b is out of both sides
    assert not any("without an error series" in x for x in notes)
    assert any("1 member-steps: excluded" in x for x in notes)


def test_verdict_reference_window_gaps_and_absent_members_are_disclosed_in_one_note():
    from types import SimpleNamespace

    import numpy as np

    from telemetry_nerd.core.verdict_ops import VerdictOps

    a_, b_ = {"service": "a"}, {"service": "b"}
    num = _result([(a_, 0, 0.5), (a_, 60_000, float("nan"))])
    den = _result([(a_, 0, 10.0), (a_, 60_000, 10.0), (b_, 0, 5.0)])
    data = {"num": num, "den": den}
    datasets = SimpleNamespace(
        meta=lambda ds: SimpleNamespace(parents=["num", "den"]), get=lambda ds: (None, data[ds])
    )
    ops = VerdictOps(SimpleNamespace(datasets=datasets))  # type: ignore[arg-type]
    stats = ops._ratio_stats()
    ops._ratio_counts("r", np.array([60_000, 120_000]), 60_000, 60.0, stats)  # a reference window
    notes: list[str] = []
    ops._ratio_notes(stats, notes)
    [note] = notes
    assert note.startswith("reference windows: members without an error series counted as 0")
    assert "1 member-steps: excluded" in note


def test_verdict_pairs_members_on_the_labels_both_series_carry():
    a5, a2 = {"service": "a", "status": "500"}, {"service": "a", "status": "502"}
    num = [(a5, 60_000, 0.25), (a2, 60_000, 0.25)]
    den = [({"service": "a"}, 60_000, 10.0)]
    a, n, notes = _ratio(num, den, [60_000])
    assert a[0] == pytest.approx(30.0) and n[0] == pytest.approx(600.0)  # errors summed
    assert not any("counted as 0" in x for x in notes)


def test_verdict_series_with_no_shared_labels_are_not_paired_not_zero():
    num = [({"code": "500"}, 60_000, 0.5)]
    den = [({"service": "a"}, 60_000, 10.0)]
    a, n, notes = _ratio(num, den, [60_000])
    assert np.isnan(a).all() and np.isnan(n).all()
    assert any("do not share member labels; not paired" in x for x in notes)


def test_verdict_absent_error_series_only_in_a_reference_window_says_so():
    a_ = {"service": "a"}
    _, _, notes = _ratio([], [(a_, 60_000, 10.0)], [60_000], shift=0)
    assert not any("reference window" in x for x in notes)
    from types import SimpleNamespace

    from telemetry_nerd.core.verdict_ops import VerdictOps

    data = {"num": _result([]), "den": _result([(a_, 0, 10.0)])}
    datasets = SimpleNamespace(
        meta=lambda ds: SimpleNamespace(parents=["num", "den"]), get=lambda ds: (None, data[ds])
    )
    ops = VerdictOps(SimpleNamespace(datasets=datasets))  # type: ignore[arg-type]
    stats = ops._ratio_stats()
    ops._ratio_counts("r", np.array([60_000]), 60_000, 60.0, stats)
    out: list[str] = []
    ops._ratio_notes(stats, out)
    assert out and out[0].endswith("(in a reference window)")


def test_error_ratio_names_absent_members_with_a_cap():
    labs = [{"service": c} for c in "abcde"]
    num = _result([(labs[0], 60_000, 0.5)])
    den = _result([(lb, 60_000, 10.0) for lb in labs])
    _, notes = error_ratio(num, den, 60_000)
    [n] = [x for x in notes if "no error series" in x]
    assert n.startswith("4 member(s)") and "(+1)" in n
    assert n.count("service") == 3
