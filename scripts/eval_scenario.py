"""Scenario eval: score a Telemetry Nerd investigation against ground truth (bead d77.3, spec §10).

  just eval <scenario>                         # offline: score a snapshot (default: the canned
                                               #   tests/fixtures/evals/<scenario>.snapshot.json)
  just eval <scenario> --snapshot S --truth T  # offline, any exported snapshot / ground truth
  just eval <scenario> --live                  # demo: run the scenario (or --attach run.json),
                                               #   start an isolated daemon on it, wait for YOU to
                                               #   investigate (no tokens), collect, score
  just eval <scenario> --live --claude         # same, but headless Claude Code investigates
                                               #   (SPENDS TOKENS: --model, --max-turns,
                                               #   --max-budget-usd and --timeout-s cap it)

Queue-sim scenarios (deploy/queue-sim/scenarios/*.yaml) are recognised by name: --live runs the
exporter and a throwaway VictoriaMetrics (no demo needed) and asks a Little's law question.

Output: build/evals/<scenario>-<UTC ts>/ with truth.json, snapshot.json, transcript.jsonl,
run.json, report.json and report.md. Exit status 0 when the d77 acceptance criteria pass.
Real Claude runs are opt-in (--claude) and never part of `just test`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from telemetry_nerd.evals import live
from telemetry_nerd.evals.questions import UNATTENDED, extra_terms, question_for
from telemetry_nerd.evals.report import markdown
from telemetry_nerd.evals.score import score
from telemetry_nerd.evals.truth import load_truth

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
RUNS = ROOT / "scenarios" / "runs"
QS_DIR = ROOT / "deploy" / "queue-sim" / "scenarios"
DEMO_VM = "http://127.0.0.1:8429"
MAX_TURNS_CAP = 40


def is_queue_sim(name: str) -> bool:
    return (QS_DIR / f"{name}.yaml").exists()


def default_truth(name: str) -> Path:
    runs = sorted(RUNS.glob(f"{name}-*.json"))
    if runs:
        return runs[-1]
    for p in (
        FIXTURES / "scenarios" / f"{name}.json",
        FIXTURES / "evals" / f"{name}.truth.json",
        ROOT / "build" / "queue-sim" / f"{name}.json",
    ):
        if p.exists():
            return p
    raise SystemExit(f"no ground truth for {name!r}: pass --truth (or run the scenario first)")


def out_dir(name: str, base: Path | None) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = (base or ROOT / "build" / "evals") / f"{name}-{ts}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_report(d: Path, truth_path: Path, snapshot: dict, run: dict | None, name: str) -> int:
    truth = load_truth(truth_path, extra_terms(name))
    rep = score(snapshot, truth)
    (d / "report.json").write_text(json.dumps({**rep.to_dict(), "run": run}, indent=1) + "\n")
    md = markdown(rep, run)
    (d / "report.md").write_text(md)
    print(md)
    print(f"report: {d / 'report.md'}")
    return 0 if rep.acceptance else 1


# --- live: demo


def demo_up() -> None:
    try:
        if httpx.get(f"{DEMO_VM}/health", timeout=2).status_code == 200:
            return
    except httpx.HTTPError:
        pass
    print("demo VictoriaMetrics not up: just demo-up", flush=True)
    subprocess.run(["just", "demo-up"], cwd=ROOT, check=True)
    raise SystemExit("demo started: let it run ~10 min for a clean baseline, then re-run")


def run_demo_scenario(name: str, scale: float | None) -> Path:
    before = set(RUNS.glob(f"{name}-*.json"))
    cmd = ["uv", "run", "scripts/scenario.py", "run", name]
    if scale:
        cmd += ["--scale", str(scale)]
    print("running scenario:", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    new = sorted(set(RUNS.glob(f"{name}-*.json")) - before)
    if not new:
        raise SystemExit("scenario run wrote no ground truth")
    return new[-1]


# --- live: queue-sim


def run_queue_sim(name: str, duration: float | None, vm_port: int, d: Path) -> Path:
    """Exporter + throwaway VM (scripts/validate_queue_sim.py's helpers); returns the truth."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import validate_queue_sim as vqs

    from telemetry_nerd.devtools.queue_sim import load_scenario, resolve_scenario

    vqs.OUT.mkdir(parents=True, exist_ok=True)
    sc = load_scenario(resolve_scenario(name))
    arg = name
    if duration:
        from dataclasses import asdict

        raw = asdict(sc)
        raw["duration_s"] = duration
        raw["faults"] = [{**f, "instances": list(f["instances"])} for f in raw["faults"]]
        raw["instances"] = list(raw["instances"])
        (vqs.OUT / f"{name}.scenario.json").write_text(json.dumps(raw))
        arg = f"{name}.scenario.json"
    (vqs.OUT / f"{name}.truth.json").unlink(missing_ok=True)
    port = 9301
    ip = vqs.start_exporters(vm_port, {name: arg}, {name: port})
    scrape_s = int(sc.scrape_interval_s)
    scrape = d / "scrape.yml"
    scrape.write_text(
        f"scrape_configs:\n  - job_name: qs-{name}\n    scrape_interval: {scrape_s}s\n"
        f"    static_configs:\n      - targets: ['{ip}:{port}']\n"
        f"        labels: {{scenario: {name}}}\n"
    )
    vqs.start_vm(vm_port, scrape, scrape_s, network=True)
    horizon = duration or sc.duration_s
    print(f"queue-sim {name} on VM :{vm_port}; running {horizon:.0f}s ...", flush=True)
    vqs.wait_finished({name: sc}, horizon + 120)
    time.sleep(2 * scrape_s + 3)
    truth = d / "truth.json"
    shutil.copy2(vqs.OUT / f"{name}.truth.json", truth)
    return truth


def stop_queue_sim(vm_port: int) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import validate_queue_sim as vqs

    vqs.stop_vm(vm_port)


# --- main


def live_run(a: argparse.Namespace) -> int:
    name = a.scenario
    d = out_dir(name, a.out)
    qs = is_queue_sim(name)
    vm_port = a.vm_port
    daemon = None
    try:
        if qs:
            truth_path = Path(a.attach) if a.attach else run_queue_sim(name, a.duration, vm_port, d)
            source_url = f"http://127.0.0.1:{vm_port}"
        else:
            demo_up()
            truth_path = Path(a.attach) if a.attach else run_demo_scenario(name, a.scale)
            source_url = DEMO_VM
        if truth_path.resolve() != (d / "truth.json").resolve():
            shutil.copy2(truth_path, d / "truth.json")
        truth = load_truth(truth_path, extra_terms(name))
        port = live.free_port()
        url = f"http://127.0.0.1:{port}"
        data = Path(tempfile.mkdtemp(prefix="tn-eval-"))
        daemon = live.start_daemon(
            ROOT, source_url, data / "data", port, d / "daemon.log", "victoriametrics"
        )
        print(f"daemon {url} (data {data})", flush=True)
        if not a.no_learn:
            learned = live.learn_source(url)
            print(f"learned source: {str(learned)[:200]}", flush=True)
        question = a.question or question_for(truth)
        run: dict = {"question": question, "daemon_url": url, "truth": str(truth_path)}
        if a.claude:
            if a.max_turns > MAX_TURNS_CAP:
                raise SystemExit(f"--max-turns is capped at {MAX_TURNS_CAP}")
            work = data / "cwd"
            work.mkdir()
            plugin = live.stage_plugin(ROOT, data / "plugin")
            cmd = live.claude_command(
                f"/telemetry-nerd:investigate {question}",
                plugin,
                model=a.model,
                max_turns=a.max_turns,
                max_budget_usd=a.max_budget_usd,
                append_system=UNATTENDED,
            )
            (d / "command.json").write_text(json.dumps(cmd, indent=1))
            print(f"claude ({a.model}, <= {a.max_turns} turns, <= ${a.max_budget_usd}): "
                  f"{question}", flush=True)  # fmt: skip
            res = live.run_claude(
                cmd, live.claude_env(ROOT, url), work, d / "transcript.jsonl", a.timeout_s
            )
            run.update(res)
            run["model"] = a.model
            print(
                f"claude done: cost ${res.get('total_cost_usd')}, turns {res.get('num_turns')}, "
                f"{res['duration_s']}s, aborted={res['aborted']}",
                flush=True,
            )
        else:
            print(
                f"\nInvestigate now (no tokens are spent by the harness):\n"
                f"  TN_DAEMON_URL={url} claude   then   /telemetry-nerd:investigate {question}\n"
                f"  UI: {url}\nPress Enter when done (or wait {a.wait_s}s).",
                flush=True,
            )
            if a.wait_s:
                time.sleep(a.wait_s)
            else:
                input()
        snap = live.collect(url)
        if run.get("result"):
            snap["transcript"] = {"result": run["result"]}
        (d / "snapshot.json").write_text(json.dumps(snap, indent=1) + "\n")
        slim = {k: v for k, v in run.items() if k not in ("tools", "usage")}
        (d / "run.json").write_text(json.dumps(run, indent=1, default=str) + "\n")
        return write_report(d, d / "truth.json", snap, slim, name)
    finally:
        live.stop(daemon)
        if qs and not a.attach and not a.keep:
            stop_queue_sim(vm_port)


def offline(a: argparse.Namespace) -> int:
    name = a.scenario
    snap_path = Path(a.snapshot) if a.snapshot else FIXTURES / "evals" / f"{name}.snapshot.json"
    if not snap_path.exists():
        raise SystemExit(f"no snapshot {snap_path}: pass --snapshot (an exported snapshot.json)")
    truth_path = Path(a.truth) if a.truth else default_truth(name)
    if a.truth is None and snap_path.parent.name.startswith(name + "-"):
        cand = snap_path.parent / "truth.json"  # a live run's own directory
        truth_path = cand if cand.exists() else truth_path
    if a.truth is None and "fixtures" in snap_path.parts:
        fx = FIXTURES / "scenarios" / f"{name}.json"
        qs = FIXTURES / "evals" / f"{name}.truth.json"
        truth_path = fx if fx.exists() else qs if qs.exists() else truth_path
    snap = json.loads(snap_path.read_text())
    if a.transcript:
        lines = Path(a.transcript).read_text().splitlines()
        snap["transcript"] = {"result": live.parse_stream(lines).get("result", "")}
    d = out_dir(name, a.out)
    shutil.copy2(truth_path, d / "truth.json")
    (d / "snapshot.json").write_text(json.dumps(snap, indent=1) + "\n")
    print(f"truth: {truth_path}\nsnapshot: {snap_path}", flush=True)
    return write_report(d, truth_path, snap, None, name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("scenario")
    ap.add_argument("--snapshot", help="offline: workspace snapshot JSON to score")
    ap.add_argument("--truth", help="ground-truth JSON (default: latest run, else the fixture)")
    ap.add_argument("--transcript", help="offline: stream-json transcript (final answer text)")
    ap.add_argument("--out", type=Path, help="output base directory (default build/evals)")
    ap.add_argument("--live", action="store_true", help="run against a live daemon")
    ap.add_argument("--attach", help="live: use this finished run's ground truth, run nothing")
    ap.add_argument("--scale", type=float, help="live demo: scenario --scale")
    ap.add_argument("--duration", type=float, help="live queue-sim: override duration_s")
    ap.add_argument("--vm-port", type=int, default=8431, help="live queue-sim: throwaway VM port")
    ap.add_argument("--keep", action="store_true", help="live queue-sim: keep the VM afterwards")
    ap.add_argument("--no-learn", action="store_true", help="live: skip source_learn")
    ap.add_argument("--question", help="override the neutral question")
    ap.add_argument("--wait-s", type=float, default=0, help="live without --claude: wait N s")
    ap.add_argument("--claude", action="store_true", help="live: headless Claude (SPENDS TOKENS)")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--max-budget-usd", type=float, default=5.0)
    ap.add_argument("--timeout-s", type=float, default=1800)
    a = ap.parse_args(argv)
    if a.claude and not a.live:
        ap.error("--claude needs --live")
    return live_run(a) if a.live else offline(a)


if __name__ == "__main__":
    sys.exit(main())
