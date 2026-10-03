"""Scenario definitions, schedule, fault derivation, verification and ground-truth writing.

No network: the VM fetcher is injected and flagd/podman are never touched.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("scenario_script", ROOT / "scripts/scenario.py")
sc = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(sc)  # type: ignore[union-attr]


def minimal(**over):
    doc = {
        "id": "x",
        "description": "d",
        "timing": {"baseline_s": 100, "cooldown_s": 50},
        "steps": [
            {"at": 200, "set": {"f": "off"}},
            {"at": 0, "set": {"f": "on"}},
        ],
        "faults": [{"flag": "f", "services": ["svc"], "sticky": True}],
        "ground_truth": {
            "root_cause_service": "svc",
            "affected": {"origin": ["svc"], "propagated": [], "unaffected_control": []},
            "expected_signals": [
                {
                    "id": "s1",
                    "service": "svc",
                    "metric": "m",
                    "query": "q",
                    "direction": "up",
                    "min_abs_delta": 1.0,
                }
            ],
        },
    }
    doc.update(over)
    return doc


def test_shipped_scenarios_parse_and_ids_match_files():
    found = sc.list_scenarios()
    assert len(found) >= 3
    for s in found:
        assert sc.load_scenario(s["id"])["id"] == s["id"]
        # every flag a scenario sets exists in the vendored flagd file with that variant
    flags = json.loads((ROOT / "deploy/demo/flagd/demo.flagd.json").read_text())["flags"]
    for s in found:
        for step in s["steps"]:
            for name, variant in step.get("set", {}).items():
                assert variant in flags[name]["variants"], (s["id"], name, variant)


def test_parse_sorts_steps_and_plan_scales():
    s = sc.parse_scenario(minimal())
    assert [x["at"] for x in s["steps"]] == [0, 200]
    p = sc.plan(s, scale=0.5)
    assert p["baseline_s"] == 50
    assert [x["t"] for x in p["steps"]] == [50, 150]
    assert p["end_s"] == 150 + 25


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda d: d.pop("timing"), "missing 'timing'"),
        (lambda d: d.update(steps=[]), "non-empty"),
        (lambda d: d.update(steps=[{"at": 0, "set": {"f": True}}]), "quoted string"),
        (lambda d: d.update(steps=[{"at": 0}]), "exactly one"),
        (lambda d: d.update(steps=[{"at": 0, "restart": ["c"]}]), "no flag 'set'"),
        (
            lambda d: d["ground_truth"]["expected_signals"][0].update(direction="sideways"),
            "direction",
        ),
    ],
)
def test_parse_rejects_bad_definitions(mutate, msg):
    doc = minimal()
    mutate(doc)
    with pytest.raises(sc.ScenarioError, match=msg):
        sc.parse_scenario(doc)


def test_yaml_on_off_must_be_quoted(tmp_path):
    (tmp_path / "x.yml").write_text(
        "id: x\ndescription: d\ntiming: {baseline_s: 1, cooldown_s: 1}\n"
        "steps:\n  - {at: 0, set: {f: on}}\n"
        "ground_truth: {root_cause_service: s, affected: {}, expected_signals: []}\n"
    )
    with pytest.raises(sc.ScenarioError, match="quoted string"):
        sc.load_scenario("x", tmp_path)


def applied(restart_at=None):
    recs = [
        {"kind": "set", "flags": {"f": "on"}, "applied_at": 1000.0, "served_at": {"f": 1002.0}},
        {"kind": "set", "flags": {"f": "off"}, "applied_at": 1300.0, "served_at": {"f": 1301.0}},
    ]
    if restart_at:
        recs.append({"kind": "restart", "containers": ["c"], "applied_at": restart_at})
    return recs


def test_derive_faults_uses_actual_times_and_sticky_restart():
    s = sc.parse_scenario(minimal())
    (f,) = sc.derive_faults(s, applied())
    assert (f["start"], f["end"], f["effect_end"]) == (1000.0, 1300.0, 1300.0)
    assert f["services"] == ["svc"] and f["variant"] == "on" and f["start_served_at"] == 1002.0
    (g,) = sc.derive_faults(s, applied(restart_at=1310.0))
    assert g["end"] == 1300.0 and g["effect_end"] == 1310.0  # sticky: effect ends at restart
    assert sc.fault_window([g]) == {"start": 1000.0, "end": 1310.0}


def test_flag_never_turned_off_has_open_end():
    s = sc.parse_scenario(minimal())
    (f,) = sc.derive_faults(s, applied()[:1])
    assert f["end"] is None and f["effect_end"] is None


@pytest.mark.parametrize(
    "direction,base,fault,kw,ok",
    [
        ("up", 0.0, 0.9, {"min_abs_delta": 0.5}, True),
        ("up", 0.0, 0.3, {"min_abs_delta": 0.5}, False),
        ("up", 1.0, 1.5, {"min_ratio": 2.0}, False),
        ("up", 0.6, 1.8, {"min_ratio": 2.0}, True),
        ("down", 5.0, 1.0, {"min_ratio": 2.0}, True),
        ("down", 5.0, 4.5, {"min_ratio": 2.0}, False),
        ("flat", 0.0, 0.01, {"max_abs_delta": 0.05}, True),
        ("flat", 0.0, 0.5, {"max_abs_delta": 0.05}, False),
    ],
)
def test_judge(direction, base, fault, kw, ok):
    assert sc.judge({"direction": direction, **kw}, base, fault)[0] is ok


def test_judge_no_data_fails():
    assert sc.judge({"direction": "up"}, None, 1.0)[0] is False


BOUNDS = {"run_start": 0.0, "fault_start": 300.0, "fault_end": 600.0, "run_end": 780.0}


def fake_fetch(values_by_ts):
    def fetch(query, start, end, step=15):
        return [{"metric": {}, "values": [[ts, str(v)] for ts, v in values_by_ts]}]

    return fetch


def test_verify_signal_windows_and_settle():
    # 0 before the fault, a ramp-up that only counts after the 120s settle, 1.0 after
    pts = [(t, 0.0) for t in range(0, 300, 15)] + [
        (t, 0.0 if t < 420 else 1.0) for t in range(300, 600, 15)
    ]
    sig = {
        "id": "s",
        "service": "svc",
        "metric": "m",
        "query": "q",
        "direction": "up",
        "min_abs_delta": 0.5,
    }
    r = sc.verify_signal(sig, BOUNDS, 120, fetch=fake_fetch(pts))
    assert r["passed"] and r["baseline_mean"] == 0.0 and r["fault_mean"] == 1.0
    assert r["fault_samples"] == len(range(420, 600, 15))
    assert r["series"][0] == [0.0, 0.0]


def test_verify_signal_absent_baseline_counts_as_zero():
    pts = [(t, 2.0) for t in range(450, 600, 15)]  # series only exists during the fault
    base = {"id": "s", "service": "svc", "metric": "m", "query": "q", "direction": "up"}
    assert sc.verify_signal(base, BOUNDS, 120, fetch=fake_fetch(pts))["passed"]
    strict = {**base, "absent_is_zero": False}
    assert not sc.verify_signal(strict, BOUNDS, 120, fetch=fake_fetch(pts))["passed"]


def test_series_points_drops_nan_and_sums_series():
    res = [
        {"values": [[1, "1"], [2, "NaN"]]},
        {"values": [[1, "2"], [2, "3"]]},
    ]
    assert sc.series_points(res) == [(1.0, 3.0), (2.0, 3.0)]


def test_ground_truth_roundtrip(tmp_path):
    s = sc.parse_scenario(minimal())
    run = {
        "started_at": 900.0,
        "baseline_end": 1000.0,
        "finished_at": 1400.0,
        "completed": True,
        "scale": 1.0,
        "applied": applied(),
    }
    ver = [{"id": "s1", "service": "svc", "passed": True, "detail": "ok", "series": [[1.0, 2.0]]}]
    gt = sc._isoify(sc.build_ground_truth(s, run, ver))
    path = sc.write_ground_truth(gt, sc.output_path("x", 900.0, tmp_path))
    assert path.name == "x-19700101T001500Z.json"
    back = json.loads(path.read_text())
    assert back["version"] == sc.GROUND_TRUTH_VERSION and back["scenario"] == "x"
    assert back["fault_window"]["start"] == 1000.0
    assert back["fault_window"]["start_iso"] == "1970-01-01T00:16:40Z"
    assert back["root_cause"]["service"] == "svc"
    assert back["affected_services"]["origin"] == ["svc"]
    assert back["tolerance"]["start_s"] == 150
    assert back["endpoints"]["victoriametrics"].startswith("http://127.0.0.1:")
    assert back["verification"]["all_passed"] is True
    assert back["expected_signals"][0]["id"] == "s1"
    assert back["faults"][0]["start_iso"] == "1970-01-01T00:16:40Z"


def test_hook_point_registered_for_queue_sim():
    assert isinstance(sc.HOOKS, dict)  # queue-sim (bead 1h9.17) registers `HOOKS[name]`
