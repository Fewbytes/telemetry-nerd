"""Visual context encoding (bead 2as.15): bounds, thresholds, reference series, reframings."""

import json

import pytest
from mcp import Client

from telemetry_nerd.catalog.models import validate_value
from telemetry_nerd.catalog.relations import validate_relation
from telemetry_nerd.charts.context_lines import (
    line_expr,
    line_matchers,
    parse_matchers,
    percent_of_limit,
    swap_metric,
)
from telemetry_nerd.charts.spec import ChartSpec
from telemetry_nerd.core.panel_payloads import limit_payload
from telemetry_nerd.core.profiles import ProfileRefused
from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import make_service
from .test_ycontext import Recording

HOST = [
    "node_memory_MemTotal_bytes",
    "node_memory_MemFree_bytes",
    "node_memory_MemAvailable_bytes",
    "node_network_receive_bytes_total",
    "node_network_speed_bytes",
    "node_hwmon_temp_celsius",
    "node_hwmon_temp_max_celsius",
    "node_hwmon_temp_crit_celsius",
]
K8S = [
    "container_memory_working_set_bytes",
    "container_spec_memory_limit_bytes",
    "kube_pod_container_resource_requests",
    "container_cpu_usage_seconds_total",
    "container_spec_cpu_quota",
    "container_spec_cpu_period",
]
APP = ["app_latency_seconds", "app_client_latency_seconds", "app_other_seconds"]


async def make(tmp_path, names):
    d = Discovery(tuple(MetricInfo(n) for n in names), (), {}, None, 1.0, (), False)
    src = Recording(name="default", discovery=d)
    s = make_service(tmp_path, src)
    await s.learn("default")
    s.src = src  # type: ignore[attr-defined]

    async def no_profile(source, expr, force=False):
        raise ProfileRefused("no profile in this test")

    s.profiles.ensure = no_profile  # type: ignore[method-assign]
    return s


@pytest.fixture
async def host(tmp_path):
    return await make(tmp_path, HOST + K8S + APP)


async def ctx_for(svc, expr):
    ds = (await svc.query(expr, start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "q?", raw_ok=True).panel.id
    return pid, await svc.y_context(pid)


# --- pure helpers ---------------------------------------------------------------------


def test_matchers_parse_and_narrow_to_the_join_labels():
    ms = parse_matchers('{namespace="a",pod=~"b.*",id="/x,y"}')
    assert ms == [("namespace", "=", '"a"'), ("pod", "=~", '"b.*"'), ("id", "=", '"/x,y"')]
    assert parse_matchers("") == [] and parse_matchers("{broken") == []
    got = line_matchers(
        '{namespace="a",pod=~"b.*",id="/x"}',
        {"join_on": ["namespace", "pod"], "matchers": {"resource": "memory"}},
    )
    assert got == '{namespace="a",pod=~"b.*",resource="memory"}'
    assert line_matchers('{a="1"}', {}) == '{a="1"}'  # no join_on: every label carries over
    assert line_matchers("", {}) == ""


def test_line_expr_derived_targets_and_unlimited():
    assert line_expr("size", {}, '{m="/"}') == 'size{m="/"}'
    got = line_expr(
        "q", {"expr": "q / p", "zero_is_unlimited": True, "join_on": ["pod"]}, '{pod="x",id="y"}'
    )
    assert got == '(q{pod="x"} / p{pod="x"}) > 0'


def test_percent_and_swap():
    assert percent_of_limit("m", "t", None) == "100 * (m) / (t)"
    assert percent_of_limit("m", "t", ["pod"]) == "100 * (m) / on(pod) group_left() (t)"
    assert swap_metric('a{x="1"}', "a", "b") == 'b{x="1"}'
    assert swap_metric("abc{x='1'}", "a", "b") is None  # not a prefix match


# --- validation -------------------------------------------------------------------------


def test_bound_relation_params_are_validated():
    validate_relation(
        "threshold_by",
        "t",
        "crit",
        {"join_on": ["chip"], "matchers": {"r": "x"}, "tone": "bad", "label": "crit"},
    )
    validate_relation("bounded_by", "u", "l", {"applies_to": "rate", "zero_is_unlimited": True})
    for kind, params, why in [
        ("bounded_by", {"nope": 1}, "not understood"),
        ("bounded_by", {"tone": "bad"}, "only applies to threshold_by"),
        ("threshold_by", {"tone": "red"}, "tone must be"),
        ("bounded_by", {"join_on": []}, "join_on"),
        ("bounded_by", {"matchers": {"a": 1}}, "matchers"),
        ("bounded_by", {"applies_to": "sum"}, "applies_to"),
        ("same_quantity", {"join_on": ["a"]}, "takes no params"),
    ]:
        with pytest.raises(ValueError, match=why):
            validate_relation(kind, "a", "b", params)


def test_thresholds_claim_validation():
    ok = [{"value": 0.5, "label": "SLO", "tone": "bad"}]
    assert validate_value("thresholds", ok) == ok
    for bad in [
        [],
        "x",
        [{"value": "a", "label": "x"}],
        [{"value": 1}],
        [{"value": 1, "label": "x", "tone": "red"}],
    ]:
        with pytest.raises(ValueError):
            validate_value("thresholds", bad)


# --- packs and learn -----------------------------------------------------------------------


async def test_learn_writes_pack_context_with_params_and_needs_every_target(
    tmp_path, tmp_path_factory
):
    svc = await make(tmp_path, ["node_network_receive_bytes_total"])  # no speed metric
    assert svc.ws.relations.relations("catalog", "default") == []
    svc = await make(tmp_path_factory.mktemp("b"), HOST)
    [r] = svc.ws.relations.relations(
        "catalog", "default", metric="node_network_receive_bytes_total"
    )
    assert (r.kind, r.object, r.winner.origin) == ("bounded_by", "node_network_speed_bytes", "pack")
    assert r.winner.params["applies_to"] == "rate" and r.winner.params["join_on"] == [
        "instance",
        "device",
    ]
    kinds = {
        x.object: x.kind
        for x in svc.ws.relations.relations("catalog", "default", metric="node_hwmon_temp_celsius")
    }
    assert kinds["node_hwmon_temp_crit_celsius"] == "threshold_by"


async def test_derived_target_needs_every_metric_it_uses(tmp_path, tmp_path_factory):
    svc = await make(tmp_path, ["container_cpu_usage_seconds_total", "container_spec_cpu_quota"])
    assert svc.ws.relations.relations("catalog", "default") == []  # no period: no CPU limit
    svc = await make(tmp_path_factory.mktemp("b"), K8S)
    objs = {
        r.object
        for r in svc.ws.relations.relations(
            "catalog", "default", metric="container_cpu_usage_seconds_total"
        )
    }
    assert objs == {"container_spec_cpu_quota"}


# --- resolver -----------------------------------------------------------------------------


async def test_a_rate_is_bounded_by_link_speed_under_the_shared_labels(host):
    pid, ctx = await ctx_for(
        host, 'rate(node_network_receive_bytes_total{instance="a",device="eth0",job="n"}[5m])'
    )
    assert 'node_network_speed_bytes{instance="a",device="eth0"}' in host.src.exprs
    [line] = ctx.lines
    assert (line.kind, line.metric, line.origin, line.label) == (
        "limit",
        "node_network_speed_bytes",
        "pack",
        "link speed",
    )
    assert line.confidence and "pack node_exporter" in line.basis
    assert ctx.limit == line
    assert ChartSpec.model_validate(host.workspace.get_panel(pid).spec).y.range_mode == "reference"


async def test_a_rate_bound_does_not_apply_to_the_running_total(host):
    _, ctx = await ctx_for(host, "node_network_receive_bytes_total")
    assert ctx.lines == []


async def test_container_memory_has_limit_and_request_lines(host):
    expr = 'container_memory_working_set_bytes{namespace="n",pod="p",container="c",id="/z"}'
    _, ctx = await ctx_for(host, expr)
    sent = host.src.exprs
    assert '(container_spec_memory_limit_bytes{namespace="n",pod="p",container="c"}) > 0' in sent
    assert (
        'kube_pod_container_resource_requests{namespace="n",pod="p",container="c",resource="memory"}'
        in sent
    )
    by_kind = {ln.kind: ln for ln in ctx.lines}
    assert (
        by_kind["limit"].label == "memory limit (OOM kill)" and by_kind["threshold"].tone == "info"
    )
    assert ctx.lines[0].kind == "limit"  # limits first, then thresholds


async def test_cpu_limit_is_a_derived_target_on_the_rate(host):
    _, ctx = await ctx_for(
        host, 'rate(container_cpu_usage_seconds_total{namespace="n",pod="p",container="c"}[5m])'
    )
    want = '(container_spec_cpu_quota{namespace="n",pod="p",container="c"} / container_spec_cpu_period{namespace="n",pod="p",container="c"}) > 0'
    assert want in host.src.exprs
    assert [ln.label for ln in ctx.lines] == ["CPU limit (cores)"]


async def test_sensor_thresholds_carry_tone_and_do_not_move_the_limit(host):
    _, ctx = await ctx_for(host, 'node_hwmon_temp_celsius{instance="a",chip="c",sensor="t1"}')
    assert {ln.label: ln.tone for ln in ctx.lines} == {
        "sensor max": "warn",
        "sensor critical": "bad",
    }
    assert ctx.limit is None  # a threshold is drawn; it is not a physical bound on the y range


async def test_a_constant_threshold_claim_is_drawn_with_who_said_so(host):
    host.ws.catalog_claim(
        "default", "app_latency_seconds", "thresholds",
        [{"value": 0.25, "label": "SLO p99", "tone": "bad"}], "user", "user",
    )  # fmt: skip
    _, ctx = await ctx_for(host, "app_latency_seconds")
    [line] = ctx.lines
    assert (line.value, line.hi, line.tone, line.origin, line.dataset) == (
        0.25,
        0.25,
        "bad",
        "user",
        None,
    )
    assert line.label == "SLO p99"


async def test_same_quantity_is_a_reference_series_and_capped(host):
    host.ws.relate(
        "default",
        "app_latency_seconds",
        "same_quantity",
        "app_client_latency_seconds",
        "user",
        "user",
    )
    host.ws.relate(
        "default",
        "app_latency_seconds",
        "same_quantity",
        "app_other_seconds",
        "claude",
        "claude",
        confidence=0.7,
    )
    host.ws.relate(
        "default",
        "app_latency_seconds",
        "same_quantity",
        "node_memory_MemFree_bytes",
        "claude",
        "claude",
        confidence=0.7,
    )
    _, ctx = await ctx_for(host, "app_latency_seconds")
    assert [ln.kind for ln in ctx.lines] == ["reference", "reference"]  # at most two
    assert ctx.limit is None


async def test_all_hard_bounds_join_the_reference_range(host):
    host.ws.relate(
        "default", "app_latency_seconds", "bounded_by", "app_other_seconds", "user", "user"
    )
    host.ws.relate(
        "default", "app_latency_seconds", "bounded_by", "app_client_latency_seconds", "user", "user"
    )
    _, ctx = await ctx_for(host, "app_latency_seconds")
    assert sorted(ln.metric for ln in ctx.lines if ln.kind == "limit") == [
        "app_client_latency_seconds",
        "app_other_seconds",
    ]
    assert ctx.limit is not None


async def test_a_failing_target_is_a_note_not_an_error(host):
    host.src.fail_on = "node_memory_MemTotal_bytes"
    _, ctx = await ctx_for(host, "node_memory_MemAvailable_bytes")
    assert ctx.lines == [] and any(n.startswith("limit_unavailable") for n in ctx.notes)


async def test_missing_target_raises_one_gap(tmp_path):
    svc = await make(tmp_path, ["node_network_receive_bytes_total"])
    for _ in range(2):
        await ctx_for(svc, "rate(node_network_receive_bytes_total[5m])")
    [gap] = svc.ws.objects.list_gaps()
    assert gap.suggestion.name == "node_network_speed_bytes" and "link speed" in gap.missing_signal
    assert "node_network_receive_bytes_total" in gap.needed_for


# --- reframings ---------------------------------------------------------------------------


async def test_free_memory_offers_available_memory(host):
    _, ctx = await ctx_for(host, 'node_memory_MemFree_bytes{instance="a"}')
    [r] = [r for r in ctx.reframes if r.kind == "substitute"]
    assert r.expr == 'node_memory_MemAvailable_bytes{instance="a"}'
    assert "page cache" in r.reason and "pack node_exporter" in r.basis


async def test_used_memory_offers_a_percent_of_its_limit(host):
    _, ctx = await ctx_for(host, 'node_memory_MemAvailable_bytes{instance="a"}')
    assert {x.kind for x in ctx.reframes} == {"percent_of_limit", "headroom"}
    r = next(x for x in ctx.reframes if x.kind == "percent_of_limit")
    assert r.unit == "%"
    assert (
        r.expr
        == '100 * (node_memory_MemAvailable_bytes{instance="a"}) / (node_memory_MemTotal_bytes{instance="a"})'
    )


async def test_no_substitute_when_the_source_lacks_the_replacement(tmp_path):
    svc = await make(tmp_path, ["node_memory_MemFree_bytes"])
    _, ctx = await ctx_for(svc, "node_memory_MemFree_bytes")
    assert ctx.reframes == []


async def test_accepting_a_reframing_makes_a_new_panel_and_leaves_the_original(host):
    pid, _ = await ctx_for(host, 'node_memory_MemFree_bytes{instance="a"}')
    before = host.workspace.get_panel(pid).spec
    res = await host.reframe(pid, 0, "user")
    new = host.workspace.get_panel(res.panel.id)
    assert new.id != pid and host.workspace.get_panel(pid).spec == before
    assert (
        new.spec["auto"]["transform"] == "reframe"
        and new.spec["auto"]["source_dataset"] == host.workspace.get_panel(pid).dataset_ids[0]
    )
    assert "MemAvailable" in host.datasets.meta(new.dataset_ids[0]).expr
    assert "reframed from" in new.spec["auto"]["reason"]
    with pytest.raises(ValueError, match="out of range"):
        await host.reframe(pid, 9, "user")


async def test_percent_reframing_sets_the_unit(host):
    pid, _ = await ctx_for(host, "node_memory_MemAvailable_bytes")
    res = await host.reframe(pid, 0, "user")
    assert res.panel.spec["y"]["unit"] == "%"


# --- payload and tools -----------------------------------------------------------------------


async def test_overlay_payload_carries_every_line_with_provenance(host):
    pid, ctx = await ctx_for(host, 'node_hwmon_temp_celsius{instance="a",chip="c",sensor="t1"}')
    meta = host.datasets.meta(host.workspace.get_panel(pid).dataset_ids[0])
    out = limit_payload(host.datasets, ctx, meta, {}, 200)
    assert out["available"] and len(out["lines"]) == 2
    crit = next(x for x in out["lines"] if x["label"] == "sensor critical")
    assert (crit["tone"], crit["origin"], crit["kind"]) == ("bad", "pack", "threshold")
    assert crit["series"] and "pack node_exporter" in crit["basis"]


async def test_mcp_show_proposes_reframings_and_reframe_accepts_one(host):
    async with Client(build_mcp(host, "http://x")) as c:
        ds = (
            await host.query(
                'node_memory_MemFree_bytes{instance="a"}', start="now-2h", end="now-1h"
            )
        )["dataset"]
        shown = json.loads(
            (
                await c.call_tool(
                    "show", {"dataset": ds, "question": "Is memory tight?", "raw": True}
                )
            )
            .content[0]
            .text
        )
        prop = shown["reframings"][0]
        assert prop["index"] == 0 and "page cache" in prop["reason"]
        done = await c.call_tool("reframe", {"panel": shown["panel"], "index": 0})
        assert (
            not done.is_error
            and json.loads(done.content[0].text)["reframed_from"] == shown["panel"]
        )
        bad = await c.call_tool("reframe", {"panel": shown["panel"], "index": 5})
        assert bad.is_error and "out of range" in bad.content[0].text


def test_a_context_stored_with_one_limit_still_loads_as_a_line():
    from telemetry_nerd.charts.spec import YContext

    old = {
        "natural_lo": 0.0,
        "limit": {"metric": "size", "dataset": "d1", "hi": 9.0, "basis": "bounded_by"},
        "notes": [],
    }
    ctx = YContext.model_validate(old)
    assert [ln.metric for ln in ctx.lines] == ["size"] and ctx.limit == ctx.lines[0]
    assert ctx.model_dump()["limit"]["hi"] == 9.0  # still serialised for the UI
    assert YContext().limit is None
