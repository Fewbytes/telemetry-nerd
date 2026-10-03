# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Scheduled flagd fault scenarios on the local OTel demo, with ground-truth JSON.

  uv run scripts/scenario.py list
  uv run scripts/scenario.py show payment-failure          # the resolved schedule
  uv run scripts/scenario.py run payment-failure [--scale 0.3] [--dry-run] [--force]

Needs the demo up (`just demo-up`). A run is: health check -> baseline (warm-up) -> the
scheduled flag changes in real time -> cool-down -> cleanup (every flag off, sticky services
restarted) -> verification of every expected signal against VictoriaMetrics (baseline window
vs fault window) -> `scenarios/runs/<id>-<UTC ts>.json`.

Scenario definitions: `scenarios/*.yml` (schema below). The output schema is documented in
docs/demo.md ("Scenario ground truth"); `GROUND_TRUTH_VERSION` is bumped on breaking changes.

Scenario YAML
  id, description
  timing:   baseline_s, cooldown_s            (the fault window is defined by the steps)
  steps:    - {at: <s after baseline end>, set: {flag: variant, ...}}   # flags
            - {at: <s>, restart: [container, ...]}                      # e.g. tn-demo-ad-1
            - {at: <s>, hook: name}                                     # extension point
  sticky_restart: [containers restarted at cleanup if no step did]
  faults:   - {flag, variant, services...}   (derived from steps when omitted)
  ground_truth: root_cause_service, root_cause_flag, root_cause_summary,
            affected: {origin, propagated, unaffected_control}, tolerance_s, settle_s,
            expected_signals: [{id, service, description, metric, query, direction
              (up|down|flat), min_abs_delta, min_ratio, max_abs_delta(flat), absent_is_zero}]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"
RUNS_DIR = SCENARIO_DIR / "runs"
GROUND_TRUTH_VERSION = 1

VM_URL = os.environ.get("DEMO_VM_URL", "http://127.0.0.1:8429")
OFREP_URL = os.environ.get("DEMO_OFREP_URL", "http://127.0.0.1:8016")
FRONTEND_URL = "http://127.0.0.1:18080"
STEP_S = 15  # evidence range-query step

DIRECTIONS = {"up", "down", "flat"}
HOOKS: dict[str, object] = {}  # name -> callable(scenario, run_state); queue-sim hook point


class ScenarioError(ValueError):
    pass


# ---------------------------------------------------------------- definitions


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ScenarioError(msg)


def parse_scenario(doc: dict, source: str = "<scenario>") -> dict:
    """Validate and normalise a scenario definition (pure; no network)."""
    _require(isinstance(doc, dict), f"{source}: top level must be a mapping")
    for k in ("id", "description", "timing", "steps", "ground_truth"):
        _require(k in doc, f"{source}: missing {k!r}")
    timing = doc["timing"]
    for k in ("baseline_s", "cooldown_s"):
        _require(
            isinstance(timing.get(k), (int, float)) and timing[k] >= 0, f"{source}: timing.{k}"
        )
    steps = doc["steps"]
    _require(isinstance(steps, list) and steps, f"{source}: steps must be a non-empty list")
    norm = []
    for i, s in enumerate(steps):
        _require(
            isinstance(s, dict) and isinstance(s.get("at"), (int, float)),
            f"{source}: step {i}: 'at'",
        )
        kinds = [k for k in ("set", "restart", "hook") if k in s]
        _require(len(kinds) == 1, f"{source}: step {i}: exactly one of set/restart/hook")
        if "set" in s:
            _require(
                isinstance(s["set"], dict) and s["set"],
                f"{source}: step {i}: set must be a mapping",
            )
            for k, v in s["set"].items():  # YAML 1.1 turns bare on/off into booleans
                _require(
                    isinstance(v, str),
                    f"{source}: step {i}: variant of {k} must be a quoted string, got {v!r}",
                )
        if "restart" in s:
            _require(
                isinstance(s["restart"], list) and s["restart"], f"{source}: step {i}: restart"
            )
        norm.append(dict(s))
    norm.sort(key=lambda s: s["at"])
    # Fault window = first step that turns a flag on .. the last step that turns a flag off/restarts.
    _require(any("set" in s for s in norm), f"{source}: no flag 'set' step")
    gt = doc["ground_truth"]
    for k in ("root_cause_service", "affected", "expected_signals"):
        _require(k in gt, f"{source}: ground_truth.{k}")
    for k in ("origin", "propagated", "unaffected_control"):
        _require(
            isinstance(gt["affected"].get(k, []), list), f"{source}: affected.{k} must be a list"
        )
    ids = set()
    for sig in gt["expected_signals"]:
        for k in ("id", "service", "metric", "query", "direction"):
            _require(k in sig, f"{source}: signal {sig.get('id')!r} missing {k!r}")
        _require(sig["direction"] in DIRECTIONS, f"{source}: signal {sig['id']}: direction")
        _require(sig["id"] not in ids, f"{source}: duplicate signal id {sig['id']}")
        ids.add(sig["id"])
    out = dict(doc)
    out["steps"] = norm
    return out


def load_scenario(name: str, directory: Path = SCENARIO_DIR) -> dict:
    path = directory / f"{name}.yml"
    if not path.exists():
        raise ScenarioError(f"unknown scenario {name!r}; try `list`")
    sc = parse_scenario(yaml.safe_load(path.read_text()), str(path))
    if sc["id"] != name:
        raise ScenarioError(f"{path}: id {sc['id']!r} must equal the file name")
    return sc


def list_scenarios(directory: Path = SCENARIO_DIR) -> list[dict]:
    out = []
    for p in sorted(directory.glob("*.yml")):
        out.append(parse_scenario(yaml.safe_load(p.read_text()), str(p)))
    return out


def plan(sc: dict, scale: float = 1.0) -> dict:
    """Relative schedule in seconds from run start (baseline_s + step.at), scaled."""
    base = sc["timing"]["baseline_s"] * scale
    steps = [{**s, "t": base + s["at"] * scale} for s in sc["steps"]]
    last = max(s["t"] for s in steps)
    return {
        "baseline_s": base,
        "steps": steps,
        "end_s": last + sc["timing"]["cooldown_s"] * scale,
    }


def derive_faults(sc: dict, applied: list[dict]) -> list[dict]:
    """One entry per flag with its *actual* on/off times, from the applied-step records.

    A flag is on from the first apply with a non-off variant until the apply that returns it to
    off. If the scenario lists `sticky_restart` containers and a restart step ran, the effect
    ends at that restart, not at the flag-off.
    """
    meta = {f["flag"]: f for f in sc.get("faults", [])}
    open_: dict[str, dict] = {}
    faults: list[dict] = []
    for rec in applied:
        if rec["kind"] != "set":
            continue
        for flag, variant in rec["flags"].items():
            if variant == "off":
                if flag in open_:
                    f = open_.pop(flag)
                    f["end"] = rec["applied_at"]
                    f["end_served_at"] = rec.get("served_at", {}).get(flag)
                    faults.append(f)
            elif flag not in open_:
                open_[flag] = {
                    "flag": flag,
                    "variant": variant,
                    "start": rec["applied_at"],
                    "start_served_at": rec.get("served_at", {}).get(flag),
                    **{k: v for k, v in meta.get(flag, {}).items() if k not in ("flag", "variant")},
                }
    for f in open_.values():  # never turned off by a step: cleanup did it (or the run aborted)
        f["end"] = None
        faults.append(f)
    restarts = [r for r in applied if r["kind"] == "restart"]
    for f in faults:
        f["effect_end"] = f["end"]
        if f.get("sticky"):
            after = [
                r["applied_at"] for r in restarts if f["end"] is None or r["applied_at"] >= f["end"]
            ]
            if after:
                f["effect_end"] = min(after)
    return faults


def fault_window(faults: list[dict]) -> dict:
    starts = [f["start"] for f in faults]
    ends = [f["effect_end"] for f in faults if f.get("effect_end") is not None]
    return {
        "start": min(starts) if starts else None,
        "end": max(ends) if ends else None,
    }


# ---------------------------------------------------------------- VictoriaMetrics


def _get_json(url: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
        return json.load(r)


def vm_instant(query: str, ts: float | None = None) -> list[dict]:
    p = {"query": query}
    if ts is not None:
        p["time"] = f"{ts:.0f}"
    return _get_json(f"{VM_URL}/api/v1/query", p)["data"]["result"]


def vm_range(query: str, start: float, end: float, step: int = STEP_S) -> list[dict]:
    p = {"query": query, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": str(step)}
    return _get_json(f"{VM_URL}/api/v1/query_range", p)["data"]["result"]


def series_points(result: list[dict]) -> list[tuple[float, float]]:
    """Collapse a range-query result to one (ts, value) list (sums series sharing a timestamp)."""
    acc: dict[float, float] = {}
    for s in result:
        for ts, v in s["values"]:
            f = float(v)
            if math.isfinite(f):
                acc[float(ts)] = acc.get(float(ts), 0.0) + f
    return sorted(acc.items())


# ---------------------------------------------------------------- verification


def window_mean(points: list[tuple[float, float]], lo: float, hi: float, absent_is_zero: bool):
    vals = [v for ts, v in points if lo <= ts <= hi]
    if not vals:
        return (0.0, 0) if absent_is_zero else (None, 0)
    return sum(vals) / len(vals), len(vals)


def judge(sig: dict, baseline: float | None, fault: float | None) -> tuple[bool, str]:
    """Pass/fail for one expectation given window means."""
    if baseline is None or fault is None:
        return False, "no data in the baseline or the fault window"
    delta = fault - baseline
    ratio = fault / baseline if baseline > 0 else math.inf if fault > 0 else 1.0
    d = sig["direction"]
    min_abs = sig.get("min_abs_delta", 0.0)
    min_ratio = sig.get("min_ratio", 1.0)
    if d == "up":
        ok = delta > 0 and delta >= min_abs and ratio >= min_ratio
        return (
            ok,
            f"baseline {baseline:.4g} -> fault {fault:.4g} (delta {delta:+.4g}, x{ratio:.3g})",
        )
    if d == "down":
        ok = delta < 0 and -delta >= min_abs and (baseline == 0 or ratio <= 1 / min_ratio)
        return (
            ok,
            f"baseline {baseline:.4g} -> fault {fault:.4g} (delta {delta:+.4g}, x{ratio:.3g})",
        )
    ok = abs(delta) <= sig.get("max_abs_delta", 0.0)
    return ok, f"baseline {baseline:.4g} -> fault {fault:.4g} (delta {delta:+.4g}), expected flat"


def verify_signal(sig: dict, bounds: dict, settle_s: float, fetch=vm_range) -> dict:
    """Query the signal over [run_start, run_end]; compare baseline vs fault window means."""
    series = fetch(sig["query"], bounds["run_start"], bounds["run_end"])
    pts = series_points(series)
    absent = sig.get("absent_is_zero", True)
    b_lo, b_hi = bounds["run_start"], bounds["fault_start"]
    f_lo, f_hi = bounds["fault_start"] + settle_s, bounds["fault_end"]
    b, nb = window_mean(pts, b_lo, b_hi, absent)
    f, nf = window_mean(pts, f_lo, f_hi, absent)
    ok, detail = judge(sig, b, f)
    return {
        "id": sig["id"],
        "service": sig["service"],
        "metric": sig["metric"],
        "query": sig["query"],
        "direction": sig["direction"],
        "baseline_mean": b,
        "baseline_samples": nb,
        "fault_mean": f,
        "fault_samples": nf,
        "passed": ok,
        "detail": detail,
        "series": [[ts, round(v, 6)] for ts, v in pts],
    }


def build_ground_truth(sc: dict, run: dict, verification: list[dict]) -> dict:
    gt = sc["ground_truth"]
    faults = derive_faults(sc, run["applied"])
    win = fault_window(faults)
    return {
        "version": GROUND_TRUTH_VERSION,
        "scenario": sc["id"],
        "description": sc["description"],
        "run": {
            "started_at": run["started_at"],
            "baseline_end": run["baseline_end"],
            "finished_at": run["finished_at"],
            "completed": run["completed"],
            "scale": run["scale"],
        },
        "fault_window": win,
        "faults": faults,
        "applied": run["applied"],
        "root_cause": {
            "service": gt["root_cause_service"],
            "flag": gt.get("root_cause_flag"),
            "summary": gt.get("root_cause_summary", ""),
        },
        "affected_services": {
            "origin": gt["affected"].get("origin", []),
            "propagated": gt["affected"].get("propagated", []),
            "unaffected_control": gt["affected"].get("unaffected_control", []),
        },
        "tolerance": {
            "start_s": gt.get("tolerance_s", {}).get("start", 150),
            "end_s": gt.get("tolerance_s", {}).get("end", 180),
            "note": "Observable onset lags the flag change: demo exports metrics and span-metrics "
            "every 60s and queries use 2m rate windows (expect 1-3 min of lag); judge annotations against fault_window.start/end within these tolerances.",
        },
        "settle_s": gt.get("settle_s", 120),
        "expected_signals": [{k: v for k, v in sig.items()} for sig in gt["expected_signals"]],
        "endpoints": {
            "victoriametrics": VM_URL,
            "flagd_ofrep": OFREP_URL,
            "frontend": FRONTEND_URL,
        },
        "source_labels": {"service": "service_name"},
        "verification": {
            "all_passed": all(v["passed"] for v in verification) if verification else False,
            "results": verification,
        },
    }


def output_path(sc_id: str, started: float, directory: Path = RUNS_DIR) -> Path:
    ts = datetime.fromtimestamp(started, UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"{sc_id}-{ts}.json"


def write_ground_truth(gt: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gt, indent=2) + "\n")
    return path


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- runner (side effects)


def _flagd():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import demo_flag

    return demo_flag


def served_variant(flag: str) -> str | None:
    r = _flagd().evaluate(flag)
    return r.get("variant")


def apply_flags(flags: dict[str, str]) -> dict:
    df = _flagd()
    doc = df.load()
    for name, variant in flags.items():
        if name not in doc["flags"]:
            raise ScenarioError(f"unknown flag {name!r}")
        if variant not in doc["flags"][name]["variants"]:
            raise ScenarioError(
                f"{name}: variant {variant!r} not in {list(doc['flags'][name]['variants'])}"
            )
        doc["flags"][name]["defaultVariant"] = variant
    t_apply = time.time()
    df.save(doc)
    served: dict[str, float | None] = {}
    deadline = time.time() + 30
    pending = dict(flags)
    while pending and time.time() < deadline:
        for name, variant in list(pending.items()):
            if served_variant(name) == variant:
                served[name] = time.time()
                del pending[name]
        if pending:
            time.sleep(1)
    return {"applied_at": t_apply, "served_at": served}


def flags_off() -> None:
    df = _flagd()
    doc = df.load()
    changed = False
    for f in doc["flags"].values():
        if "off" in f["variants"] and f["defaultVariant"] != "off":
            f["defaultVariant"] = "off"
            changed = True
    if changed:
        df.save(doc)


def restart_containers(names: list[str]) -> None:
    for n in names:
        subprocess.run(["podman", "restart", n], check=True, capture_output=True)


def health_check(force: bool = False) -> list[str]:
    """Returns the list of problems (empty = healthy baseline)."""
    problems = []
    try:
        _get_json(f"{VM_URL}/api/v1/query", {"query": "1"}, timeout=5)
    except Exception as e:  # noqa: BLE001
        return [f"VictoriaMetrics not reachable at {VM_URL}: {e}; run `just demo-up`"]
    try:
        _get_json(f"{OFREP_URL}/ofrep/v1/evaluate/flags/paymentFailure", None, timeout=5)
    except urllib.error.HTTPError:
        pass
    except Exception as e:  # noqa: BLE001
        problems.append(f"flagd OFREP not reachable at {OFREP_URL}: {e}")
    try:
        rate = vm_instant('sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[2m]))')
        r = float(rate[0]["value"][1]) if rate else 0.0
        if r <= 0:
            problems.append("no recent frontend traffic in VM (is the load generator running?)")
        err = vm_instant(
            'sum(rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR",'
            'service_name!~"load-generator"}[2m]))'
        )
        e = float(err[0]["value"][1]) if err else 0.0
        if e > 0.05:
            problems.append(
                f"baseline is not clean: {e:.3f} error spans/s before any fault (load-generator excluded)"
            )
        fl = _flagd().load()["flags"]
        on = [n for n, f in fl.items() if f["defaultVariant"] != "off" and "off" in f["variants"]]
        if on:
            problems.append(f"flags not off at start: {on}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"health query failed: {e}")
    return problems


def run_scenario(
    sc: dict, scale: float = 1.0, force: bool = False, out_dir: Path = RUNS_DIR
) -> Path:
    pl = plan(sc, scale)
    problems = health_check()
    if problems:
        for p in problems:
            print(f"health: {p}", file=sys.stderr)
        if not force:
            raise SystemExit("baseline unhealthy (use --force to run anyway)")
    flags_off()  # known start state (health already warned)
    started = time.time()
    run = {
        "started_at": started,
        "baseline_end": started + pl["baseline_s"],
        "finished_at": None,
        "completed": False,
        "scale": scale,
        "applied": [],
    }
    print(
        f"[{iso(started)}] {sc['id']}: baseline {pl['baseline_s']:.0f}s, total {pl['end_s']:.0f}s"
    )
    try:
        for step in pl["steps"]:
            wait = started + step["t"] - time.time()
            if wait > 0:
                time.sleep(wait)
            if "set" in step:
                rec = {"kind": "set", "flags": step["set"], **apply_flags(step["set"])}
            elif "restart" in step:
                restart_containers(step["restart"])
                rec = {"kind": "restart", "containers": step["restart"], "applied_at": time.time()}
            else:
                hook = HOOKS.get(step["hook"])
                if hook is None:
                    raise ScenarioError(f"hook {step['hook']!r} not registered")
                hook(sc, run)  # type: ignore[operator]
                rec = {"kind": "hook", "hook": step["hook"], "applied_at": time.time()}
            run["applied"].append(rec)
            print(
                f"[{iso(rec['applied_at'])}] {rec['kind']} {step.get('set') or step.get('restart') or step.get('hook')}"
            )
        wait = started + pl["end_s"] - time.time()
        if wait > 0:
            time.sleep(wait)
        run["completed"] = True
    finally:
        flags_off()
        sticky = sc.get("sticky_restart", [])
        if sticky and not any(r["kind"] == "restart" for r in run["applied"]):
            restart_containers(sticky)
            run["applied"].append(
                {
                    "kind": "restart",
                    "containers": sticky,
                    "applied_at": time.time(),
                    "cleanup": True,
                }
            )
        run["finished_at"] = time.time()
    return finish(sc, run, out_dir)


def finish(sc: dict, run: dict, out_dir: Path = RUNS_DIR) -> Path:
    faults = derive_faults(sc, run["applied"])
    win = fault_window(faults)
    bounds = {
        "run_start": run["started_at"],
        "fault_start": win["start"] or run["baseline_end"],
        "fault_end": win["end"] or run["finished_at"],
        "run_end": run["finished_at"],
    }
    time.sleep(20)  # let the last samples land in VM
    settle = sc["ground_truth"].get("settle_s", 60)
    verification = []
    for sig in sc["ground_truth"]["expected_signals"]:
        try:
            verification.append(verify_signal(sig, bounds, settle))
        except Exception as e:  # noqa: BLE001
            verification.append(
                {
                    "id": sig["id"],
                    "service": sig["service"],
                    "passed": False,
                    "detail": f"query failed: {e}",
                    "series": [],
                }
            )
    gt = build_ground_truth(sc, run, verification)
    gt = _isoify(gt)
    path = write_ground_truth(gt, output_path(sc["id"], run["started_at"], out_dir))
    for v in verification:
        print(f"{'PASS' if v['passed'] else 'FAIL'} {v['id']}: {v['detail']}")
    print(f"ground truth: {path}")
    return path


TIME_KEYS = {
    "started_at",
    "baseline_end",
    "finished_at",
    "start",
    "end",
    "effect_end",
    "applied_at",
    "start_served_at",
    "end_served_at",
}


def _isoify(obj):
    """Add a `<key>_iso` sibling next to every epoch-seconds timestamp (harness uses either)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in TIME_KEYS and isinstance(v, (int, float)):
                out[k] = round(v, 3)
                out[k + "_iso"] = iso(v)
            elif k == "served_at" and isinstance(v, dict):
                out[k] = {n: (round(t, 3) if t else None) for n, t in v.items()}
            elif k == "series":
                out[k] = v
            else:
                out[k] = _isoify(v)
        return out
    if isinstance(obj, list):
        return [_isoify(x) for x in obj]
    return obj


# ---------------------------------------------------------------- CLI


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("name")
    rn = sub.add_parser("run")
    rn.add_argument("name")
    rn.add_argument("--scale", type=float, default=1.0, help="multiply all durations (quick tries)")
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--force", action="store_true", help="run despite an unhealthy baseline")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            for sc in list_scenarios():
                t = sc["timing"]
                fl = sorted({f for s in sc["steps"] if "set" in s for f in s["set"]})
                print(
                    f"{sc['id']:28} {t['baseline_s']}s+fault+{t['cooldown_s']}s  flags={','.join(fl)}\n    {sc['description'].strip().splitlines()[0]}"
                )
            return 0
        sc = load_scenario(a.name)
        if a.cmd == "show" or a.dry_run:
            print(json.dumps(plan(sc, getattr(a, "scale", 1.0)), indent=2))
            return 0
        run_scenario(sc, a.scale, a.force)
        return 0
    except ScenarioError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
