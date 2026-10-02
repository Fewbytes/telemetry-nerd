import asyncio

import pytest

from telemetry_nerd.charts.spec import ChartSpec, YContext
from telemetry_nerd.charts.ycontext import (
    RATE_BOUND_CONFIDENCE,
    counter_rate_metric,
    counter_rate_parts,
    limit_expr,
    natural_range,
    rate_bound,
    reframing_specs,
    selector_parts,
)
from telemetry_nerd.charts.yview import ValueStats, YView, check_view
from telemetry_nerd.core import service as service_mod
from telemetry_nerd.core.profiles import ProfileRefused
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.sources.base import SourceError

from .fakes import FakeSource, make_service


def test_selector_parts_and_counter_rate_metric():
    assert selector_parts("up") == ("up", "")
    assert selector_parts('node_fs_avail{mountpoint="/", job=~"a|b"}') == (
        "node_fs_avail",
        '{mountpoint="/", job=~"a|b"}',
    )
    assert selector_parts("sum(up)") is None and selector_parts("a / b") is None
    assert counter_rate_metric('rate(reqs_total{job="x"}[5m])') == "reqs_total"
    assert counter_rate_metric("increase(reqs_total[1h])") == "reqs_total"
    assert counter_rate_metric("sum(rate(reqs_total[5m]))") is None
    assert counter_rate_metric("rate(a[5m]) / rate(b[5m])") is None


def test_natural_range_and_limit_expr():
    assert natural_range("≥0") == (0.0, None) and natural_range("[0,1]") == (0.0, 1.0)
    assert natural_range("none") == (None, None) and natural_range(None) == (None, None)
    assert limit_expr('{mountpoint="/"}', "size_bytes") == 'size_bytes{mountpoint="/"}'


def test_counter_rate_parts_carries_the_matchers():
    assert counter_rate_parts('rate(reqs_total{job="x"}[5m])') == ("reqs_total", '{job="x"}')
    assert counter_rate_parts("increase(reqs_total[1h])") == ("reqs_total", "")
    assert counter_rate_parts("sum(rate(reqs_total[5m]))") is None


def test_rate_bound_known_metrics():
    label, expr, basis = rate_bound("node_network_receive_bytes_total", '{device="eth0"}')
    assert label == "node_network_speed_bytes"
    assert expr == 'node_network_speed_bytes{device="eth0"}'
    assert "link speed" in basis

    label, expr, basis = rate_bound("container_cpu_usage_seconds_total", '{pod="p"}')
    assert label == "container_spec_cpu_quota/container_spec_cpu_period"
    assert expr == '(container_spec_cpu_quota{pod="p"} / container_spec_cpu_period{pod="p"})'
    assert "quota" in basis.lower()

    assert rate_bound("unknown_counter_total", "") is None
    assert 0 < RATE_BOUND_CONFIDENCE < 1


def test_reframing_specs_never_apply_silently_and_carry_both_transforms():
    specs = reframing_specs(
        "node_memory_MemFree_bytes",
        'node_memory_MemTotal_bytes{instance="a"}',
        "node_memory_MemTotal_bytes",
    )
    transforms = {t for t, *_ in specs}
    assert transforms == {"headroom", "percent_of_limit"}
    headroom = next(s for s in specs if s[0] == "headroom")
    assert headroom[1] == '(node_memory_MemTotal_bytes{instance="a"}) - (node_memory_MemFree_bytes)'
    assert "no separate limit line" in headroom[3]
    pct = next(s for s in specs if s[0] == "percent_of_limit")
    assert (
        pct[1] == '100 * (node_memory_MemFree_bytes) / (node_memory_MemTotal_bytes{instance="a"})'
    )
    assert "same risk at any scale" in pct[3]


class Recording(FakeSource):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.exprs: list[str] = []
        self.fail_on: str | None = None

    async def fetch(self, expr, rng, step_ms):
        self.exprs.append(expr)
        if self.fail_on and self.fail_on in expr:
            raise SourceError("boom", hint="retry")
        return await super().fetch(expr, rng, step_ms)


NAMES = [
    "node_filesystem_avail_bytes",
    "node_filesystem_size_bytes",
    "node_network_receive_bytes_total",
    "node_network_speed_bytes",
    "container_memory_working_set_bytes",
    "container_spec_memory_limit_bytes",
    "container_cpu_usage_seconds_total",
    "container_spec_cpu_quota",
    "container_spec_cpu_period",
    "app_cache_hit_ratio",
    "app_requests_total",
    "app_odd",
]


@pytest.fixture
async def svc(tmp_path):
    d = Discovery(tuple(MetricInfo(n) for n in NAMES), (), {}, None, 1.0, (), False)
    src = Recording(name="default", discovery=d)
    s = make_service(tmp_path, src)
    await s.learn("default")
    s.src = src  # type: ignore[attr-defined]

    async def no_profile(source, expr, force=False):
        raise ProfileRefused("no profile in this test")

    s.profiles.ensure = no_profile  # type: ignore[method-assign]
    return s


async def panel(svc, expr, **kw):
    ds = (await svc.query(expr, start="now-2h", end="now-1h"))["dataset"]
    return svc.show(ds, "q?", **kw).panel.id


async def ctx_of(svc, expr):
    pid = await panel(svc, expr)
    ctx = await svc.y_context(pid)
    stored = ChartSpec.model_validate(svc.workspace.get_panel(pid).spec).y
    assert stored.context == ctx
    return ctx, stored


async def test_natural_bounds_from_the_catalog_for_a_plain_selector(svc):
    ctx, y = await ctx_of(svc, "app_cache_hit_ratio")
    assert (ctx.natural_lo, ctx.natural_hi, ctx.bounds, ctx.bounds_origin) == (
        0.0,
        1.0,
        "[0,1]",
        "rule",
    )
    assert y.range_mode == "data"  # bounds alone are not a reference range


async def test_a_user_bounds_claim_wins(svc):
    svc.ws.catalog_claim("default", "app_odd", "bounds", "[0,100]", "user", "user")
    ctx, _ = await ctx_of(svc, "app_odd")
    assert (ctx.natural_hi, ctx.bounds_origin) == (100.0, "user")


async def test_counter_rate_is_never_negative(svc):
    ctx, _ = await ctx_of(svc, "rate(app_requests_total[5m])")
    assert (ctx.natural_lo, ctx.natural_hi, ctx.bounds_origin) == (0.0, None, "counter rate")


async def test_mixed_expressions_have_no_natural_bounds(svc):
    ctx, _ = await ctx_of(svc, "app_cache_hit_ratio * 2")
    assert ctx.natural_lo is None and ctx.natural_hi is None
    assert any(n.startswith("natural_bounds_unknown") for n in ctx.notes)


async def test_physical_limit_is_the_bounding_metric_under_the_same_labels(svc):
    expr = 'node_filesystem_avail_bytes{mountpoint="/"}'
    ctx, y = await ctx_of(svc, expr)
    assert 'node_filesystem_size_bytes{mountpoint="/"}' in svc.src.exprs
    assert ctx.limit is not None and ctx.limit.metric == "node_filesystem_size_bytes"
    assert ctx.limit.hi == 2.5  # max of the bounding dataset's drawn values
    assert svc.datasets.exists(ctx.limit.dataset)  # reusable: 2as.11 draws it as a line
    assert y.range_mode == "reference"
    assert ctx.natural_lo == 0.0
    # provenance (bead 2as.15): a pack-derived bound, never a magic number
    assert ctx.limit.origin == "pack" and "node_exporter" in ctx.limit.basis
    assert ctx.limit.confidence == pytest.approx(0.85)
    # a resolved bound always comes with a reframing suggestion, never applied silently
    transforms = {r.transform for r in ctx.reframings}
    assert transforms == {"headroom", "percent_of_limit"}
    assert all(r.origin == "rule" for r in ctx.reframings)
    headroom = next(r for r in ctx.reframings if r.transform == "headroom")
    assert headroom.expr == f'(node_filesystem_size_bytes{{mountpoint="/"}}) - ({expr})'


async def test_container_memory_working_set_is_bounded_by_the_configured_limit(svc):
    expr = 'container_memory_working_set_bytes{pod="p",container="c"}'
    ctx, _ = await ctx_of(svc, expr)
    assert ctx.limit is not None and ctx.limit.metric == "container_spec_memory_limit_bytes"
    assert 'container_spec_memory_limit_bytes{pod="p",container="c"}' in svc.src.exprs


async def test_network_rate_is_bounded_by_the_link_speed(svc):
    expr = 'rate(node_network_receive_bytes_total{device="eth0"}[5m])'
    ctx, _ = await ctx_of(svc, expr)
    assert ctx.limit is not None and ctx.limit.metric == "node_network_speed_bytes"
    assert 'node_network_speed_bytes{device="eth0"}' in svc.src.exprs
    assert (ctx.limit.origin, ctx.limit.confidence) == ("rule", RATE_BOUND_CONFIDENCE)
    assert "link speed" in ctx.limit.basis
    # a usable number carries its own context once reframed; no separate limit line needed
    assert any(r.transform == "percent_of_limit" for r in ctx.reframings)


async def test_cpu_rate_is_bounded_by_cfs_quota_over_period(svc):
    expr = 'rate(container_cpu_usage_seconds_total{pod="p"}[5m])'
    ctx, _ = await ctx_of(svc, expr)
    assert ctx.limit is not None
    assert ctx.limit.metric == "container_spec_cpu_quota/container_spec_cpu_period"
    assert (
        '(container_spec_cpu_quota{pod="p"} / container_spec_cpu_period{pod="p"})' in svc.src.exprs
    )
    assert ctx.limit.origin == "rule"


async def test_zero_limit_is_treated_as_unlimited_not_a_bound(tmp_path):
    class ZeroLimitSource(Recording):
        async def fetch(self, expr, rng, step_ms):
            res = await super().fetch(expr, rng, step_ms)
            if "container_spec_memory_limit_bytes" not in expr:
                return res
            import pyarrow as pa

            b = res.buckets
            zero = pa.table(
                {
                    "ts_ms": b["ts_ms"],
                    "series_id": b["series_id"],
                    "avg": pa.array([0.0] * b.num_rows, type=pa.float64()),
                    "min": pa.array([0.0] * b.num_rows, type=pa.float64()),
                    "max": pa.array([0.0] * b.num_rows, type=pa.float64()),
                    "count": b["count"],
                },
                schema=b.schema,
            )
            return type(res)(buckets=zero, series=res.series)

    d = Discovery(tuple(MetricInfo(n) for n in NAMES), (), {}, None, 1.0, (), False)
    src = ZeroLimitSource(name="default", discovery=d)
    s = make_service(tmp_path, src)
    await s.learn("default")

    async def no_profile(source, expr, force=False):
        raise ProfileRefused("no profile in this test")

    s.profiles.ensure = no_profile  # type: ignore[method-assign]
    ctx, _ = await ctx_of(s, 'container_memory_working_set_bytes{pod="p"}')
    assert ctx.limit is None
    assert any("unlimited" in n for n in ctx.notes)


async def test_limit_degrades_to_a_note_when_the_source_fails(svc):
    pid = await panel(svc, "node_filesystem_avail_bytes")
    svc.src.fail_on = "node_filesystem_size_bytes"
    ctx = await svc.y_context(pid)
    assert ctx.limit is None and any(n.startswith("limit_unavailable") for n in ctx.notes)
    assert ChartSpec.model_validate(svc.workspace.get_panel(pid).spec).y.range_mode == "data"


async def test_limit_does_not_apply_to_a_rate_of_the_bounded_metric(svc):
    svc.ws.relate("default", "app_requests_total", "bounded_by", "app_odd", "user", "user")
    ctx, _ = await ctx_of(svc, "rate(app_requests_total[5m])")
    assert ctx.limit is None and any("not its rate" in n for n in ctx.notes)


class Prof:
    def __init__(self, lo, hi, extremes=True):
        self.extremes = extremes
        self.window_ms = 30 * 86_400_000

        class R:
            envelope_lo, envelope_hi, p005, p995 = lo, hi, lo + 1, hi - 1

        self.pooled = R()


async def test_profile_range_becomes_the_normal_range(svc):
    async def ensure(source, expr, force=False):
        return Prof(10.0, 90.0)

    svc.profiles.ensure = ensure  # type: ignore[method-assign]
    ctx, y = await ctx_of(svc, "app_odd")
    assert (ctx.profile.lo, ctx.profile.hi) == (10.0, 90.0) and "30d" in ctx.profile.label
    assert y.range_mode == "reference"


async def test_profile_without_true_extremes_uses_the_robust_quantiles(svc):
    async def ensure(source, expr, force=False):
        return Prof(10.0, 90.0, extremes=False)

    svc.profiles.ensure = ensure  # type: ignore[method-assign]
    ctx, _ = await ctx_of(svc, "app_odd")
    assert (ctx.profile.lo, ctx.profile.hi) == (11.0, 89.0)


async def test_slow_profile_is_pending_not_blocking(svc, monkeypatch):
    async def slow(source, expr, force=False):
        await asyncio.sleep(5)

    svc.profiles.ensure = slow  # type: ignore[method-assign]
    monkeypatch.setattr(service_mod, "PROFILE_WAIT_S", 0.05)
    ctx, _ = await ctx_of(svc, "app_odd")
    assert ctx.profile is None and any(n.startswith("profile_pending") for n in ctx.notes)


async def test_refused_profile_is_reported(svc):
    ctx, _ = await ctx_of(svc, "app_odd")
    assert any(n.startswith("profile_unavailable") and "no profile" in n for n in ctx.notes)


async def test_non_time_panels_get_no_context(svc):
    ds = (await svc.query("app_odd", start="now-2h", end="now-1h"))["dataset"]
    pid = svc.show(ds, "q?").panel.id
    p = svc.workspace.get_panel(pid)
    spec = ChartSpec.model_validate(p.spec)
    spec.layers[0].mark = "heatmap"
    svc.workspace.set_spec(pid, spec.model_dump())
    assert await svc.y_context(pid) is None


def test_semantic_needs_natural_bounds():
    st = ValueStats(1.0, 2.0, False)
    sem = YView(mode="semantic", label="natural bounds")
    with pytest.raises(ValueError, match="no natural bounds"):
        check_view(sem, st, None)
    with pytest.raises(ValueError, match="no natural bounds"):
        check_view(sem, st, YContext())
    assert check_view(sem, st, YContext(natural_lo=0.0)) == []
    assert check_view(YView(mode="reference", label="reference range"), st, None) == []


async def test_reference_and_semantic_views_can_be_selected(svc):
    pid = await panel(svc, "app_cache_hit_ratio")
    await svc.y_context(pid)
    p = svc.ws.select_y_view(pid, "user", mode="semantic")
    assert p.spec["y"]["selected"]["mode"] == "semantic"
    p = svc.ws.select_y_view(pid, "user", mode="reference")
    assert p.spec["y"]["selected"]["label"] == "reference range"
    odd = await panel(svc, "app_odd")
    with pytest.raises(ValueError, match="no natural bounds"):
        svc.ws.select_y_view(odd, "user", mode="semantic")  # app_odd has no bounds claim
