"""check_littles_law end to end over a simulated source (czt.2)."""

import asyncio
import json

import pytest
from mcp import Client

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.workspace.models import FindingIn

from .fakes import NOW, make_service
from .littles_sim import ARRIVALS, CONCURRENCY, LATENCY, SimSource, simulate

START = NOW - 3_600_000
ROLES = {"arrival_rate": ARRIVALS, "latency": LATENCY, "concurrency": CONCURRENCY}


def _service(tmp_path, n=1, hide=frozenset(), timer="arrival", **kw):
    sims = {f"i{i}": simulate(20 + i, rates=[(0.0, 6.0)], c=10, timer=timer) for i in range(n)}
    return make_service(tmp_path, source=SimSource(sims, START, hide=hide, **kw))


def _run(svc, **kw):
    return asyncio.run(
        svc.check_littles_law(start="now-1h", end="now", window="5m", **{**ROLES, **kw})
    )


def test_consistent_total_states_assumptions_and_cites_evidence(tmp_path):
    svc = _service(tmp_path)
    out = _run(svc)
    assert out["verdict"] == "consistent"
    assert set(out["datasets"]) == {"arrival_rate", "latency_sum", "latency_count", "concurrency"}
    assert out["window"] == "5m" and out["substep"] == "15s"
    names = {a["name"]: a["status"] for a in out["assumptions"]}
    assert set(names) >= {
        "steady_state",
        "arrivals_vs_completions",
        "label_sets",
        "units",
        "window_alignment",
        "warmup",
        "gauge_sampling",
    }
    assert names["units"] == "ok"  # _seconds from the name rule
    assert names["arrivals_vs_completions"] == "assumed"
    t = out["total"]
    assert t["ci95"][0] <= 1 <= t["ci95"][1]
    assert len(t["windows"]) == 12
    # the statistics are evidence as they are
    ev = t["evidence"][0]
    assert ev["name"] == "littles_law_ratio" and ev["dataset"] == out["datasets"]["concurrency"]
    f = svc.ws.finding_create(
        FindingIn.model_validate({
            "claim": "L matches lambda W", "evidence": [ev],
            "scope": {"source": "default", "selector": CONCURRENCY,
                      "time_range": {"start_ms": START + 300_000, "end_ms": NOW},
                      "step": "15s", "aggregation": "sum"},
        }),
        "claude",
    )  # fmt: skip
    assert f.id


def test_queries_are_rates_of_sum_and_count_never_percentiles(tmp_path):
    svc = _service(tmp_path)
    _run(svc)
    exprs = svc.sources.get("default").exprs
    assert any(f"rate({LATENCY}_sum[" in e for e in exprs)
    assert any(f"rate({LATENCY}_count[" in e for e in exprs)
    assert not any("quantile" in e for e in exprs)


def test_missing_instance_is_low_and_localised(tmp_path):
    svc = _service(tmp_path, n=3, hide={("gauge", "i2")})
    out = _run(svc, by=["instance"])
    assert out["total"]["verdict"] == "L_low"
    assert out["unmatched"] == [
        {"labels": {"instance": "i2"}, "present_in": ["arrival_rate", "latency"],
         "missing_in": ["concurrency"]}
    ]  # fmt: skip
    assert {g["labels"]["instance"]: g["verdict"] for g in out["groups"]} == {
        "i0": "consistent",
        "i1": "consistent",
    }
    assert any("i2" in h and "concurrency" in h for h in out["hints"])
    assert next(a for a in out["assumptions"] if a["name"] == "label_sets")["status"] == "flagged"


def test_unmeasured_queueing_is_high(tmp_path):
    sims = {"i0": simulate(5, rates=[(0.0, 9.5)], c=10, timer="service_start")}
    svc = make_service(tmp_path, source=SimSource(sims, START))
    out = _run(svc)
    assert out["verdict"] == "L_high"
    assert any("queueing before the timer" in h for h in out["hints"])


def test_millisecond_histogram_is_converted(tmp_path):
    name = "http_request_duration_milliseconds"
    svc = _service(tmp_path, latency_scale=1000.0, latency_name=name)
    out = _run(svc, latency=name)
    assert out["verdict"] == "consistent"
    assert "latency in ms" in next(a for a in out["assumptions"] if a["name"] == "units")["detail"]


def test_unknown_latency_unit_is_assumed_and_flagged(tmp_path):
    name = "http_request_duration"
    svc = _service(tmp_path, latency_name=name)
    out = _run(svc, latency=name)
    assert "latency_unit_assumed" in out["caveats"]
    assert next(a for a in out["assumptions"] if a["name"] == "units")["status"] == "assumed"


@pytest.mark.parametrize(
    "latency",
    [
        f'{LATENCY}{{quantile="0.99"}}',
        "http_request_latency_p99",
        f"histogram_quantile(0.99, rate({LATENCY}_bucket[5m]))",
    ],
)
def test_percentile_latency_is_refused_with_a_hint(tmp_path, latency):
    svc = _service(tmp_path)
    with pytest.raises(ValueError, match="MEAN latency"):
        _run(svc, latency=latency)


def test_latency_without_sum_and_count_in_the_catalog_is_refused(tmp_path):
    svc = _service(tmp_path)
    names = [ARRIVALS, CONCURRENCY, "rpc_latency_p95_seconds"]
    svc.ws.catalog_learn(
        "default",
        Discovery(tuple(MetricInfo(n) for n in names), (), {}, None, 1.0, (), False),
        "system",
    )
    with pytest.raises(ValueError, match="_sum"):
        _run(svc, latency="rpc_latency_seconds")


def _learn(svc):
    names = [ARRIVALS, CONCURRENCY, f"{LATENCY}_bucket", f"{LATENCY}_sum", f"{LATENCY}_count"]
    svc.ws.catalog_learn(
        "default",
        Discovery(
            tuple(MetricInfo(n) for n in names), (), {LATENCY: "classic"}, None, 1.0, (), False
        ),
        "system",
    )


def test_binding_supplies_roles_and_join_on(tmp_path):
    svc = _service(tmp_path, n=2)
    _learn(svc)
    roles = {**ROLES, "latency": f"{LATENCY}_bucket"}  # the family's catalogued member
    svc.ws.bind("default", "littles_law", "api", roles, "user", "user", join_on=["instance"])
    out = asyncio.run(svc.check_littles_law(binding="api", start="now-1h", end="now", window="5m"))
    assert out["binding"]["key"] == "api"
    assert {g["labels"]["instance"] for g in out["groups"]} == {"i0", "i1"}


def test_missing_role_is_refused_with_a_suggestion(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(ValueError, match="concurrency.*active_requests"):
        asyncio.run(svc.check_littles_law(arrival_rate=ARRIVALS, latency=LATENCY, start="now-1h"))


def test_show_littles_panel(tmp_path):
    svc = _service(tmp_path, n=2)
    out = _run(svc, by=["instance"])
    panel = svc.show(out["datasets"]["concurrency"], "Does L match lambda W?", mark="littles").panel
    assert set(panel.dataset_ids) == set(out["datasets"].values())
    data = svc.panel_data(panel.id, 800)
    assert data["kind"] == "littles"
    assert [s["id"] for s in data["series"]] == ["total", "instance=i0", "instance=i1"]
    w = data["series"][0]["windows"][0]
    assert {"L", "L_ci", "lambda_W", "lambda_W_ci", "ratio", "ci95", "verdict"} <= set(w)


def test_show_littles_needs_a_check_first(tmp_path):
    svc = _service(tmp_path)
    d = asyncio.run(svc.query(CONCURRENCY, start="now-1h", end="now", step="15s"))["dataset"]
    with pytest.raises(ValueError, match="check_littles_law first"):
        svc.show(d, "q", mark="littles")


def test_mcp_tool(tmp_path):
    svc = _service(tmp_path)

    async def go():
        async with Client(build_mcp(svc, "http://x")) as c:
            res = await c.call_tool(
                "check_littles_law",
                {**ROLES, "start": "now-1h", "end": "now", "window": "5m"},
            )
            return json.loads(res.content[0].text)

    out = asyncio.run(go())
    assert out["verdict"] == "consistent"
    assert out["draw"].startswith('show("')


def test_histogram_count_as_arrivals_is_completions(tmp_path):
    svc = _service(tmp_path)
    out = _run(svc, arrival_rate=f"{LATENCY}_count")
    a = next(a for a in out["assumptions"] if a["name"] == "arrivals_vs_completions")
    assert "completions" in a["detail"]
    assert out["verdict"] == "consistent"


def test_suggested_binding_from_otel_metadata_runs(tmp_path):
    """czt.1 proposes the histogram BASE name (only base names come from metadata): the check
    finds _sum/_count through the catalog's histogram family and reads the ms/s unit there."""
    from .test_binding_suggest import discovery, real

    lat = "http_server_request_duration_seconds"
    sims = {"i0": simulate(9, rates=[(0.0, 6.0)], c=10)}
    src = SimSource(sims, START, latency_name=lat)
    src.discovery = discovery(real("play_otel_metadata.json"))
    svc = make_service(tmp_path, source=src)

    async def go():
        await svc.learn("default")
        s = next(
            x
            for x in svc.ws.binding_suggest("default", "littles_law", limit=100)["suggestions"]
            if x["roles"]["latency"] == lat
        )
        svc.ws.binding_accept("default", s["id"], basis="test")
        with pytest.raises(ValueError, match="bound keys"):
            await svc.check_littles_law(binding=s["id"], start="now-1h", window="5m")
        return await svc.check_littles_law(binding=s["key"], start="now-1h", window="5m", by=[])

    out = asyncio.run(go())
    assert out["verdict"] == "consistent"
    assert any(f"rate({lat}_sum" in e for e in src.exprs)


def test_statistics_over_inputs_of_unknown_uncertainty_are_flagged(tmp_path):
    """Spec §5.3: the check folds no declared input interval into its own, so it marks its
    statistics with the inputs' status (x2x / 4jk); clean source data leaves them unmarked."""
    from dataclasses import replace

    svc = _service(tmp_path)
    out = _run(svc)
    ev = out["total"]["evidence"]
    assert ev and all("input_uncertainty" not in e["params"] for e in ev)
    assert "input_uncertainty_unknown" not in out["caveats"]
    cfg = svc.littles.last_config(out["datasets"]["concurrency"])
    lam = out["datasets"]["arrival_rate"]
    meta = svc.datasets.meta
    svc.datasets.meta = lambda d: (  # an input whose uncertainty is unknown
        replace(meta(d), source_caveats=["no_uncertainty"]) if d == lam else meta(d)
    )
    marked = svc.littles.summary(cfg)
    assert "input_uncertainty_unknown" in marked["caveats"]
    ev = marked["total"]["evidence"]
    assert ev and all(e["params"]["input_uncertainty"] == "unknown" for e in ev)
