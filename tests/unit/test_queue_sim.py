"""queue-sim engine and scenario logic (no network, virtual time)."""

import json
import re
from pathlib import Path

import numpy as np
import pytest

from telemetry_nerd.devtools.queue_sim import (
    SCENARIO_DIR,
    Fault,
    Params,
    Scenario,
    Sim,
    exact_windows,
    ground_truth,
    load_scenario,
    make_params,
)


def scenario(**kw) -> Scenario:
    base = {
        "name": "t", "description": "", "duration_s": 600, "seed": 3, "scrape_interval_s": 5,
        "service": "svc", "instances": ("a", "b"),
        "defaults": {"lambda": 4.0, "mu": 1.0, "c": 6}, "overrides": {}, "faults": (),
        "expect": {},
    }  # fmt: skip
    return Scenario(**{**base, **kw})


def metric(text: str, name: str, pod: str) -> float:
    m = re.search(rf'^{name}\{{service="svc",pod="{pod}"\}} (\S+)$', text, re.MULTILINE)
    return float(m.group(1)) if m else float("nan")


def test_littles_law_holds_in_the_sim_when_consistent():
    sim = Sim(scenario(duration_s=3000))
    sim.advance(100)
    s0 = {p: (metric(sim.render(), "http_request_duration_seconds_sum", p)) for p in "ab"}
    n0 = {p: metric(sim.render(), "http_request_duration_seconds_count", p) for p in "ab"}
    area = 0.0
    for t in np.arange(100.5, 2500, 0.5):  # time-average of the gauge
        sim.advance(float(t))
        area += metric(sim.render(), "http_server_active_requests", "a") * 0.5
    txt = sim.render()
    lam = (metric(txt, "http_requests_total", "a")) / 2500
    W = (metric(txt, "http_request_duration_seconds_sum", "a") - s0["a"]) / (
        metric(txt, "http_request_duration_seconds_count", "a") - n0["a"]
    )
    L = area / 2400
    assert L == pytest.approx(lam * W, rel=0.1)
    assert lam == pytest.approx(4.0, rel=0.1)


def test_mm1_mean_latency_matches_theory():
    sim = Sim(scenario(defaults={"lambda": 0.5, "mu": 1.0, "c": 1}, duration_s=40000, seed=5))
    sim.advance(40000)
    txt = sim.render()
    w = metric(txt, "http_request_duration_seconds_sum", "a") / metric(
        txt, "http_request_duration_seconds_count", "a"
    )
    assert w == pytest.approx(1 / (1 - 0.5), rel=0.08)  # M/M/1: W = 1/(mu - lambda)


@pytest.mark.parametrize("dist", ["exp", "lognormal", "gamma", "det"])
def test_service_distributions_have_the_requested_mean(dist):
    sim = Sim(scenario(defaults={"lambda": 0, "mu": 2.0, "dist": dist, "cv": 0.7}))
    p = make_params(Params(), {"mu": 2.0, "dist": dist, "cv": 0.7})
    xs = np.array([sim._service_time(p) for _ in range(20000)])
    assert xs.mean() == pytest.approx(0.5, rel=0.03)
    if dist in ("lognormal", "gamma"):
        assert xs.std() / xs.mean() == pytest.approx(0.7, rel=0.08)
    if dist == "det":
        assert xs.std() == 0


def test_counter_mode_arrivals_vs_completions_and_leak():
    for mode in ("arrivals", "completions"):
        sim = Sim(scenario(defaults={"lambda": 3, "mu": 1, "c": 5, "counter": mode}, seed=8))
        sim.advance(400)
        i = sim.inst["a"]
        assert i.requests == (i.arrivals if mode == "arrivals" else i.completions)
    sim = Sim(scenario(defaults={"lambda": 3, "mu": 1, "c": 5, "leak": 0.05}, seed=8))
    sim.advance(1000)
    i = sim.inst["a"]
    stuck = i.arrivals - i.completions - (i.busy + len(i.waiting))
    assert i.in_flight == i.arrivals - i.completions
    assert stuck == pytest.approx(0.05 * i.arrivals, rel=0.35)


def test_hidden_queueing_timer_excludes_wait():
    kw = {"defaults": {"lambda": 4.5, "mu": 1.0, "c": 5}, "duration_s": 6000, "seed": 2}
    out = {}
    for timer in ("arrival", "service_start"):
        sim = Sim(scenario(**{**kw, "defaults": {**kw["defaults"], "timer": timer}}))
        sim.advance(6000)
        i = sim.inst["a"]
        out[timer] = i.lat_sum / i.n_timed
    assert out["service_start"] == pytest.approx(1.0, rel=0.05)  # pure service time
    assert out["arrival"] > 1.3 * out["service_start"]  # rho 0.9: queueing is substantial


def test_pre_delay_is_in_flight_but_not_timed():
    sim = Sim(scenario(defaults={"lambda": 2.0, "mu": 1.0, "c": 20, "pre_delay": 3.0}, seed=4))
    sim.advance(3000)
    i = sim.inst["a"]
    assert i.lat_sum / i.n_timed == pytest.approx(1.0, rel=0.08)
    assert i.in_flight == pytest.approx(2.0 * (1.0 + 3.0), abs=6)  # lambda (W + pre_delay)


def test_fault_schedule_overload_builds_backlog_and_reverts():
    f = Fault("overload", 100, 200, ("a",), {"lambda": 9.0}, {})
    sim = Sim(scenario(faults=(f,), defaults={"lambda": 3.0, "mu": 1.0, "c": 5}))
    sim.advance(99)
    assert sim.inst["a"].p.lam == 3.0
    sim.advance(199)
    assert sim.inst["a"].p.lam == 9.0 and sim.inst["b"].p.lam == 3.0
    assert sim.inst["a"].in_flight > 150  # (9 - 5) x 100 s of backlog, minus noise
    sim.advance(205)
    assert sim.inst["a"].p.lam == 3.0
    sim.advance(600)
    assert sim.inst["a"].in_flight < 30  # drained at (5 - 3)/s


def test_missing_instance_gauge_is_not_exported_but_the_rest_is():
    f = Fault("missing_instance", 0, None, ("b",), {"export_gauge": False}, {})
    sim = Sim(scenario(faults=(f,)))
    sim.advance(50)
    txt = sim.render()
    assert 'http_server_active_requests{service="svc",pod="a"}' in txt
    assert 'http_server_active_requests{service="svc",pod="b"}' not in txt
    assert 'http_requests_total{service="svc",pod="b"}' in txt


def test_usl_dilation_lowers_throughput_capacity():
    plain = Sim(scenario(defaults={"lambda": 20, "mu": 1.0, "c": 8}, seed=6))
    usl = Sim(scenario(defaults={"lambda": 20, "mu": 1.0, "c": 8, "sigma": 0.1, "kappa": 0.02}))
    for s in (plain, usl):
        s.advance(300)
    # saturated: completions/s follow the USL capacity, not c x mu
    assert plain.inst["a"].completions > 1.4 * usl.inst["a"].completions


def test_exposition_is_valid_and_histogram_cumulative():
    sim = Sim(scenario())
    sim.advance(200)
    txt = sim.render()
    buckets = [
        float(m.group(1))
        for m in re.finditer(
            r'http_request_duration_seconds_bucket\{service="svc",pod="a",le="[^"]+"\} (\d+)', txt
        )
    ]
    assert buckets == sorted(buckets)
    assert buckets[-1] == metric(txt, "http_request_duration_seconds_count", "a")
    assert all(re.match(r"^(#|[a-z_]+(\{.*\})? \S+$)", ln) for ln in txt.splitlines())


def test_same_seed_is_deterministic():
    a, b = Sim(scenario(seed=9)), Sim(scenario(seed=9))
    a.advance(120)
    b.advance(120)
    assert a.render() == b.render()


def test_unknown_parameter_and_bad_enum_rejected():
    with pytest.raises(ValueError, match="unknown parameter"):
        make_params(Params(), {"lamda": 1})
    with pytest.raises(ValueError, match="timer"):
        Params(timer="start")


def test_bundled_scenarios_load_and_have_ground_truth():
    names = {p.stem for p in SCENARIO_DIR.glob("*.yaml")}
    assert {"consistent", "hidden_queueing", "missing_instance", "overload_spike"} <= names
    for p in SCENARIO_DIR.glob("*.yaml"):
        sc = load_scenario(p)
        Sim(sc).advance(10)  # parameters are valid
        gt = ground_truth(sc, 1_000_000, 1_700_000)
        json.dumps(gt)
        assert gt["expect"]["verdict"] and gt["group_by"] == "pod"
        for f in gt["faults"]:
            assert f["start_unix_ms"] == 1_000_000 + int(f["start_s"] * 1000)
            assert f["expect"]["verdict"] and f["instances"]


def test_overload_ground_truth_predicts_the_backlog_and_drain():
    gt = ground_truth(load_scenario(SCENARIO_DIR / "overload_spike.yaml"), 0)
    [f] = gt["faults"]
    assert f["kind"] == "overload" and (f["start_s"], f["end_s"]) == (300, 360)
    # (15 - 10) x 60 = 300 backlog, drained at 10 - 5 = 5/s
    assert f["effect"] == {"backlog_peak_per_instance": 300.0, "drain_end_s": 420.0}


def test_exact_windows_give_the_realised_littles_law_quantities():
    """Timer from arrival, no fault: over long windows L = lambda W holds on the realised path;
    a missing gauge counts only the exported instances (R ~ 1/2 of two equal instances)."""
    rows = exact_windows(scenario(), [(60.0, 300.0), (300.0, 600.0)], dt=0.05)
    assert all(abs(r["R"] - 1) < 0.05 for r in rows), rows
    sc = scenario(overrides={"b": {"export_gauge": False}})
    (r,) = exact_windows(sc, [(60.0, 600.0)], dt=0.05)
    assert 0.4 < r["R"] < 0.6


def _validate_script():
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("vqs", root / "scripts/validate_queue_sim.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    ("verdict", "transient", "promoted", "ok"),
    [
        ("consistent", [], [(300, 360)], True),  # vayr: consistent + a promoted load peak
        ("inconsistent_in_windows", [(300, 360, "peak")], [], True),
        ("inconsistent_in_windows", [(360, 420, "drain")], [], True),
        ("consistent", [], [], False),  # nothing special at the episode
        ("inconsistent_in_windows", [(300, 360, "peak")], [(600, 660)], False),  # a stray window
    ],
)
def test_overload_transient_is_met_by_special_windows_whatever_the_verdict(
    verdict, transient, promoted, ok
):
    from datetime import UTC, datetime

    vqs = _validate_script()
    t0 = 1_700_000_000_000
    gt = ground_truth(load_scenario(SCENARIO_DIR / "overload_spike.yaml"), t0)
    assert gt["expect"]["verdict"] == "any"
    iso_ = lambda s: datetime.fromtimestamp((t0 + s * 1000) / 1000, UTC).isoformat()
    out = {
        "verdict": verdict,
        "total": {"growing": None},
        "classification": {
            "transient": [
                {
                    "window": [iso_(a), iso_(b)],
                    "source": "special_cause",
                    "phase": ph,
                    "direction": "L_high",
                }
                for a, b, ph in transient
            ],
            "promoted": [{"window": [iso_(a), iso_(b)]} for a, b in promoted],
            "systematic": None,
        },
    }
    assert vqs.matches(gt, out)[0] is ok, vqs.matches(gt, out)
