"""check_littles_law end to end over a simulated source (czt.2)."""

import asyncio
import json

import numpy as np
import pytest
from mcp import Client

from telemetry_nerd.mcp.server import build_mcp
from telemetry_nerd.model.discovery import Discovery, MetricInfo
from telemetry_nerd.model.time import iso
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
    assert len(t["windows"][0]) == len(t["window_columns"])
    # the discrepancy comes first, whatever the verdict: absolute and relative, with the interval
    assert list(out)[:5] == ["summary", "discrepancy", "verdict", "classification", "warnings"]
    d = out["discrepancy"]
    assert d["difference"] is not None and d["difference_ci95"][0] <= d["difference"]
    assert d["relative_ci95"][0] <= d["relative"] <= d["relative_ci95"][1]
    assert d["per_window"]["windows"] == 12 and d["common_cause_rel95"]["per_window"] > 0
    assert out["summary"].startswith("Discrepancy over the range: L − λ·W = ")
    assert out["summary"].index("Discrepancy") < out["summary"].index("Verdict consistent")
    c = out["classification"]
    assert {k: c[k] for k in ("reference", "systematic", "transient", "promoted")} == {
        "reference": 1.0, "systematic": None, "transient": [], "promoted": [],
    }  # fmt: skip
    assert c["promotion"]["alpha"] == 0.05
    names = [e["name"] for e in t["evidence"]]
    assert names[:2] == ["littles_law_ratio", "littles_law_discrepancy"]
    disc = t["evidence"][1]
    assert disc["value"] == d["relative"] and disc["interval"] == d["relative_ci95"]
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
    # spec §5.4: a discrepancy inside the measurement interval is the measurement system's; the
    # label travels with the statistic into the finding and workspace_get
    assert ev["source"] == disc["source"] == "measurement_system"
    assert f.sources == ["measurement_system"]
    brief = next(x for x in svc.ws.brief()["findings"] if x["id"] == f.id)
    assert brief["sources"] == ["measurement_system"]
    assert {v["source"] for v in out["variation"]} == {"measurement_system", "common_cause"}


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
    sysd = out["classification"]["systematic"]
    assert sysd["direction"] == "L_high" and sysd["source"] == "measurement_system"
    assert sysd["windows"][0] * 2 > sysd["windows"][1]
    assert "systematic offset" in out["summary"] and "measurement system" in out["summary"]
    names = [e["name"] for e in out["total"]["evidence"]]
    assert "littles_law_systematic_offset" in names
    for t in out["classification"]["transient"]:  # each transient is cited with its window
        assert t["source"] in ("special_cause", "common_cause")
        ev = next(
            e for e in out["total"]["evidence"]
            if e["name"] == "littles_law_transient" and e["params"]["window"] == t["window"]
        )  # fmt: skip
        assert ev["value"] == t["relative"]


def test_low_traffic_warns_with_the_common_cause_scale(tmp_path):
    sims = {"i0": simulate(8, rates=[(0.0, 0.3)], c=4)}
    svc = make_service(tmp_path, source=SimSource(sims, START))
    out = _run(svc)
    (w,) = [w for w in out["warnings"] if "fluctuate" in w]
    cc = out["total"]["common_cause"]
    assert f"±{100 * cc['rel95']:.0f}% per window" in w and "N≈" in w
    assert cc["rel95"] > 0.3 and cc["source"] == "common_cause"
    assert out["discrepancy"]["ratio"] is not None  # shown, not hidden
    s = out["summary"]
    assert s.index("Discrepancy") < s.index("Verdict") < s.index("Warnings")


def test_load_spike_is_called_out_as_transient_at_a_peak(tmp_path):
    spike = [(0.0, 2.0), (1500.0, 5.0), (1800.0, 2.0)]
    sims = {"i0": simulate(602, rates=spike, c=4, counter="completions")}
    svc = make_service(tmp_path, source=SimSource(sims, START))
    out = _run(svc)
    tr = out["classification"]["transient"]
    # 83w: the peak window's deviation (L ÷ λW 1.26) is inside its measurement interval (the
    # gauge's end-of-interval reading corrected), but the backlog grew by hundreds: promoted to
    # special cause, and the output says why, with the numbers
    (p,) = out["classification"]["promoted"]
    assert p["window"][0] == iso(START + 1_500_000)  # the 5 min at rho 1.25
    assert p["source"] == "special_cause" and p["from"] == "measurement_system"
    assert p["deviation"] == "within_measurement" and "backlog grew +" in p["reason"]
    assert any("leaving steady state (special cause, promoted from measurement system)" in w
               for w in out["warnings"])  # fmt: skip
    assert not any("not a signal by itself" in w for w in out["warnings"])
    assert "promoted to special cause" in out["summary"]
    assert out["summary"].startswith("Special cause in ")
    assert f"{p['window'][0]} (at a load peak, promoted)" in out["summary"].split(". ")[0]
    assert any("toward overload" in h for h in out["hints"])
    (stat,) = [e for e in out["total"]["evidence"] if e["name"] == "littles_law_backlog_growth"]
    assert stat["source"] == "special_cause" and stat["interval"][0] <= stat["value"]
    assert stat["params"]["window"] == p["window"] and stat["params"]["z"] >= stat["params"]["k"]
    # the backlog draining after it: a transient labelled drain (inside the envelope here); the
    # grid shifted by half a window sees the drain too (its own span, `grid: offset`)
    main = [(t["window"][0], t["phase"]) for t in tr if "grid" not in t]
    assert main == [(iso(START + 1_800_000), "drain")]
    for t in tr:
        if t.get("grid"):
            assert t["phase"] == "drain" and t["window"][0] == iso(START + 1_950_000)
    # the special-cause transient carries its label onto its evidence and the variation list
    ev = [e for e in out["total"]["evidence"] if e["name"] == "littles_law_transient"]
    assert {e["source"] for e in ev} == {t["source"] for t in tr}
    assert {t["source"] for t in tr} <= {v["source"] for v in out["variation"]}


def test_no_concurrency_data_says_the_check_cannot_be_done(tmp_path):
    svc = _service(tmp_path, hide={("gauge", "i0")})
    out = _run(svc)
    assert out["summary"].startswith("Little's law cannot be checked without a concurrency")
    assert "L was not estimated" in out["summary"]
    assert out["discrepancy"]["L"] is None and out["discrepancy"]["ratio"] is None
    assert any("in-flight gauge" in h for h in out["hints"])
    assert out["variation"] == []  # nothing measured, nothing labelled


def test_unknown_unit_is_a_measurement_system_item(tmp_path):
    svc = _service(tmp_path, latency_name="http_request_duration")
    out = _run(svc, latency="http_request_duration")
    assert {"source": "measurement_system", "finding": "latency unit assumed",
            "caveat": "latency_unit_assumed"} in out["variation"]  # fmt: skip


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
    with pytest.raises(
        ValueError, match="cannot be checked without a concurrency.*active_requests"
    ):
        asyncio.run(svc.check_littles_law(arrival_rate=ARRIVALS, latency=LATENCY, start="now-1h"))


def test_show_littles_panel(tmp_path):
    svc = _service(tmp_path, n=2)
    out = _run(svc, by=["instance"])
    panel = svc.show(out["datasets"]["concurrency"], "Does L match lambda W?", mark="littles").panel
    assert set(panel.dataset_ids) == set(out["datasets"].values())
    data = svc.panel_data(panel.id, 800)
    assert data["kind"] == "littles"
    assert [s["id"] for s in data["series"]] == ["total", "instance=i0", "instance=i1"]
    s0 = data["series"][0]
    w = s0["windows"][0]
    assert {"L", "L_ci", "lambda_W", "lambda_W_ci", "ratio", "ci95", "verdict"} <= set(w)
    assert {"diff", "diff_ci", "common", "source", "reference"} <= set(w)
    assert {"reference", "systematic", "transient", "common_cause"} <= set(s0)


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


def test_spike_with_an_arrivals_counter_is_promoted_at_the_peak(tmp_path):
    """q2m: an arrivals counter partly compensates (L ~ lambda W in the peak window, within the
    measurement interval), but the backlog — the gauge and arrivals - completions — grows:
    special cause at the peak, the verdict about L = lambda W unchanged, the reason shown."""
    spike = [(0.0, 2.0), (1500.0, 5.0), (1800.0, 2.0)]
    sims = {"i0": simulate(602, rates=spike, c=4)}
    svc = make_service(tmp_path, source=SimSource(sims, START))
    out = _run(svc)
    assert out["verdict"] == "consistent"  # Little's law holds within measurement
    (p,) = out["classification"]["promoted"]
    assert p["window"][0] == iso(START + 1_500_000) and p["source"] == "special_cause"
    assert p["from"] == "measurement_system" and p["deviation"] == "within_measurement"
    (e,) = [e for e in p["evidence"] if e["kind"] == "backlog_growth"]
    assert e["significant"] and e["flow"] > 300 and e["gauge"] > 300  # both instruments see it
    assert "arrivals − completions +" in p["reason"]
    assert "Leaving steady state at a load peak — promoted to special cause (1)" in out["summary"]
    # vayr: "consistent" answers only L = lambda W overall; the summary opens with the special
    # cause so a reader does not stop at the verdict word
    s = out["summary"]
    assert s.startswith(
        f"Special cause in 1 window(s): {p['window'][0]} (at a load peak, promoted)."
    )
    assert s.index("Special cause") < s.index("Discrepancy") < s.index("Verdict consistent")
    assert any(v["source"] == "special_cause" and v.get("promoted") for v in out["variation"])
    names = [e["name"] for e in out["total"]["evidence"]]
    assert "littles_law_backlog_growth" in names
    panel = svc.show(out["datasets"]["concurrency"], "q", mark="littles").panel
    s0 = svc.panel_data(panel.id, 800)["series"][0]
    assert s0["promoted"] == [{
        "index": 5, "from": "measurement_system", "deviation": "within_measurement",
        "reason": p["reason"], "evidence": ["backlog_growth"],
    }]  # fmt: skip
    assert s0["windows"][5]["source"] == "special_cause"


def test_victoriametrics_counters_are_increase_tiles_without_lookback(tmp_path):
    """9fd/xa4: on VictoriaMetrics the counters are increase() over scrape-interval tiles (they
    partition the counter and end at the gauge's scrapes), so no lookback bias widens the
    interval; the same data read through rate() gives the same ratio with a bias bound."""
    sims = {"i0": simulate(602, rates=[(0.0, 2.0), (1500.0, 5.0), (1800.0, 2.0)], c=4)}
    vm = SimSource(sims, START, flavor="victoriametrics")
    (tmp_path / "vm").mkdir()
    (tmp_path / "prom").mkdir()
    out_vm = _run(make_service(tmp_path / "vm", source=vm))
    out_rate = _run(make_service(tmp_path / "prom", source=SimSource(sims, START)))
    assert any("increase(http_requests_total[15s])" in e for e in vm.exprs)
    assert any("increase(http_request_duration_seconds_sum[15s])" in e for e in vm.exprs)
    assert not any("rate(" in e for e in vm.exprs)
    assert out_vm["total"]["bias"]["alignment"] == 0
    assert out_rate["total"]["bias"]["alignment"] > 0
    assert out_vm["total"]["ratio"] == out_rate["total"]["ratio"]
    w_vm, w_rate = out_vm["total"]["windows"], out_rate["total"]["windows"]
    assert [w[2] for w in w_vm] == [w[2] for w in w_rate]  # same ratios
    assert all(v[4] - v[3] <= r[4] - r[3] for v, r in zip(w_vm, w_rate, strict=True))
    (wa,) = [a for a in out_vm["assumptions"] if a["name"] == "window_alignment"]
    assert "increase() over 15s tiles" in wa["detail"] and "no lookback" in wa["detail"]


def test_windows_are_anchored_at_the_requested_start_not_widened(tmp_path):
    """The range is never widened to wall-clock multiples of the window: data before `start`
    (a warm-up the caller excluded) is not judged; windows follow the data."""
    svc = _service(tmp_path)
    start = START + 100_000  # not a multiple of the window (nor of the 15 s sub-step)
    out = asyncio.run(svc.check_littles_law(start=str(start), end="now", window="5m", **ROLES))
    first = START + 105_000  # rounded up to the sub-step, never down
    assert out["range"][0] == iso(first)
    assert out["total"]["windows"][0][0] == iso(first)
    assert out["total"]["windows"][1][0] == iso(first + 300_000)


def test_scrape_interval_differing_from_the_source_resolution_is_stated(tmp_path):
    """The gauge's scrape interval is probed at the range end: scraped every 5s on a source
    configured at 15s, a 1m window is refused with the reconnect hint, and a check that runs
    states the mismatch."""
    sims = {"i0": simulate(20, rates=[(0.0, 6.0)], c=10)}
    src = SimSource(sims, START, scrape_ms=5_000)
    svc = make_service(tmp_path, source=src)
    with pytest.raises(ValueError, match=r'scraped every 5s .*resolution="5s"'):
        asyncio.run(svc.check_littles_law(start="now-1h", end="now", window="1m", **ROLES))
    out = _run(svc)
    (g,) = [a for a in out["assumptions"] if a["name"] == "gauge_sampling"]
    assert g["status"] == "flagged" and "scraped every 5s" in g["detail"]
    assert src.probes and src.probes[-1][0] == CONCURRENCY and src.probes[-1][1] == NOW


def test_an_empty_scrape_tile_is_an_increase_of_zero_not_missing_data():
    from telemetry_nerd.core.littles_ops import _fill_empty_tiles

    v = np.array([np.nan, 3.0, np.nan, 7.0, np.nan, np.nan, 2.0])
    n = np.array([0, 1, 0, 2, 0, 0, 1.0])
    c, _ = _fill_empty_tiles(v, n, gauge=False)
    g, gn = _fill_empty_tiles(v, n, gauge=True)
    # isolated gaps only: a leading gap and a two-tile gap stay missing
    assert np.array_equal(c, [np.nan, 3, 0, 7, np.nan, np.nan, 2], equal_nan=True)
    assert np.array_equal(g, [np.nan, 3, 3, 7, np.nan, np.nan, 2], equal_nan=True)
    assert np.array_equal(gn, n)  # no sample was read there


def test_few_windows_rest_the_special_cause_label_on_the_cautious_envelope(tmp_path):
    """4ahp: 4 windows give no estimate of the process's own window-to-window variation: the
    transient's label rests on the cautious envelope, both envelopes reported, and the summary
    says so."""
    m = simulate(20, rates=[(0.0, 6.0)], c=10)
    m["gauge"] = m["gauge"].astype(float)
    m["gauge"][-60:-40] *= 2.5  # one of the last four 5-minute windows
    svc = make_service(tmp_path, source=SimSource({"i0": m}, START))
    out = asyncio.run(svc.check_littles_law(start="now-20m", end="now", window="5m", **ROLES))
    assert out["total"]["common_cause"]["envelope"] == "cautious"
    assert out["total"]["common_cause"]["cautious_rel95"] >= out["total"]["common_cause"]["rel95"]
    (t,) = [t for t in out["classification"]["transient"] if t["envelope"]["deviation"] > 0.5]
    env = t["envelope"]
    assert env["label_rests_on"] == "cautious" and "spread" not in env
    assert env["cautious"]["rel95"] >= env["small_system"]["rel95"]
    assert t["source"] in ("special_cause", "undetermined")
    assert "cautious envelope" in out["summary"]
