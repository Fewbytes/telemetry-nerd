"""Validate check_littles_law against queue-sim ground truth on a REAL VictoriaMetrics (bead 317).

  uv run python scripts/validate_queue_sim.py [scenario ...] [--vm-port 8431] [--keep]

Starts a throwaway VictoriaMetrics container (own name and volume, scraping the exporters over
HTTP like production: -promscrape.config), runs the scenarios in parallel as real-time exporters
(one port each, label scenario=<name>), then starts a telemetry-nerd daemon on a free port, calls
check_littles_law over MCP per scenario and compares it with the ground-truth JSON. Writes
build/queue-sim/validate/results.json. The dev stack on :8428 is never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from mcp import Client
from mcp.types import TextContent

from telemetry_nerd.devtools.queue_sim import (
    METRIC_ACTIVE,
    METRIC_LATENCY,
    METRIC_REQUESTS,
    exact_windows,
    load_scenario,
    resolve_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "queue-sim" / "validate"
VM_IMAGE = "victoriametrics/victoria-metrics:v1.137.0"
EXPORTER_IMAGE = "ghcr.io/astral-sh/uv:python3.14-trixie-slim"


def names(port: int) -> tuple[str, str]:
    """Container and volume of the throwaway VM on `port` (one pair per port: runs on different
    ports never share or remove each other's)."""
    return f"tn-qs-vm-{port}", f"tn-qs-vmdata-{port}"


DEFAULT = [
    "consistent", "hidden_queueing", "missing_instance", "overload_spike",
    "overload_spike_completions", "leak",
]  # fmt: skip


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def start_vm(port: int, scrape_yml: Path, scrape_s: int, network: bool = False) -> None:
    CONTAINER, VOLUME = names(port)
    sh("podman", "rm", "-f", CONTAINER, check=False)
    sh("podman", "volume", "rm", "-f", VOLUME, check=False)
    net = ["--network", f"tn-qs-net-{port}"] if network else []
    sh("podman", "run", "-d", "--name", CONTAINER, *net, "-p", f"{port}:8428", "-v", f"{VOLUME}:/data",
       "-v", f"{scrape_yml}:/etc/vm/scrape.yml:ro", VM_IMAGE,
       "-storageDataPath=/data", "-retentionPeriod=10y", "-search.latencyOffset=0s",
       f"-dedup.minScrapeInterval={scrape_s}s", "-promscrape.config=/etc/vm/scrape.yml")  # fmt: skip
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("VictoriaMetrics did not become healthy")


def start_exporters(port: int, runs: dict[str, str], ports: dict[str, int]) -> str:
    """All scenarios in one container on a network of its own with the VM; returns its IP.
    Scrape targets are addressed by IP: the container DNS search domain adds ~5 s per lookup."""
    net, name = f"tn-qs-net-{port}", f"tn-qs-exp-{port}"
    sh("podman", "rm", "-f", name, check=False)
    sh("podman", "network", "create", net, check=False)
    cmds = " & ".join(
        f"uv run /repo/scripts/queue_sim.py "
        f"{('/out/' + arg) if arg.endswith('.json') else arg} --port {ports[n]} "
        f"--truth /out/{n}.truth.json"
        for n, arg in runs.items()
    )
    sh("podman", "run", "-d", "--name", name, "--network", net, "-v", f"{ROOT}:/repo:ro",
       "-v", f"{OUT}:/out", "-e", "UV_CACHE_DIR=/tmp/uvc", EXPORTER_IMAGE,
       "sh", "-c", f"uv run --with numpy --with pyyaml python -c 1 && {{ {cmds} & wait; }}")  # fmt: skip
    fmt = f'{{{{(index .NetworkSettings.Networks "{net}").IPAddress}}}}'
    return sh("podman", "inspect", name, "--format", fmt).stdout.strip()


def wait_finished(scs: dict, timeout_s: float) -> None:
    """Until every exporter has written its ground truth with an end time."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        done = 0
        for n in scs:
            try:
                done += (
                    json.loads((OUT / f"{n}.truth.json").read_text())["ended_unix_ms"] is not None
                )
            except (OSError, ValueError, KeyError):
                pass
        if done == len(scs):
            return
        time.sleep(5)
    raise RuntimeError("exporters did not finish in time")


def scrape_health(port: int, scenario: str, scrape_s: int) -> str:
    """Scrapes that failed or were slow (harness integrity: missing scrapes are not the check's)."""

    def one(q: str) -> float:
        r = httpx.get(f"http://127.0.0.1:{port}/api/v1/query", params={"query": q}).json()
        res = r["data"]["result"]
        return float(res[0]["value"][1]) if res else 0.0

    sel = f'{{scenario="{scenario}"}}'
    total = one(f"count_over_time(up{sel}[1h])")
    down = one(f"count_over_time((up{sel} == 0)[1h])")
    slow = one(f"count_over_time((scrape_duration_seconds{sel} > {scrape_s / 5})[1h])")
    return f"{total:.0f} scrapes, {down:.0f} failed, {slow:.0f} slower than {scrape_s / 5:g}s"


def stop_vm(port: int) -> None:
    CONTAINER, VOLUME = names(port)
    sh("podman", "rm", "-f", f"tn-qs-exp-{port}", check=False)
    sh("podman", "rm", "-f", CONTAINER, check=False)
    sh("podman", "volume", "rm", "-f", VOLUME, check=False)
    sh("podman", "network", "rm", "-f", f"tn-qs-net-{port}", check=False)


def analyse(truth: dict, out: dict) -> dict:
    cls = out.get("classification") or {}
    total = out.get("total") or {}
    sysd = cls.get("systematic")
    return {
        "expected": truth["expect"],
        "verdict": out.get("verdict"),
        "systematic": sysd and {"ratio": sysd.get("ratio"), "ci95": sysd.get("ci95")},
        "drifting": bool(
            ((total.get("classification") or {}).get("systematic") or {}).get("drifting")
        )
        or bool((total.get("growing") or {}).get("growing")),
        "transient": [
            {
                "window": t["window"],
                "direction": t["direction"],
                "ratio": t["ratio"],
                "phase": t["phase"],
                "source": t["source"],
            }
            for t in cls.get("transient", [])
        ],
        "windows": out.get("window"),
        "discrepancy": {k: out["discrepancy"].get(k) for k in ("relative", "relative_ci95")},
    }


def _ms(iso_text: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso_text).timestamp() * 1000)


def matches(truth: dict, out: dict) -> tuple[bool, str]:
    """Does the check agree with the ground truth? (verdict and classification)

    none: consistent; nothing systematic, transient or promoted.
    systematic: the verdict and a systematic offset; transient windows only in the offset's own
      direction and not draining (excursions of the same fault, e.g. a hidden queue growing in a
      burst: "in-flight time the latency timer does not see"), no promotions.
    growing: the verdict and L - lambda W growing over the windows (a drifting offset).
    transient: special cause (a transient beyond the envelope, or a promoted load-peak window) in
      a window overlapping the episode (the fault and its drain); nothing special outside it (+-
      one window); `peak` only on windows overlapping the load, `drain` only after it started;
      verdict inconsistent_in_windows, or consistent when the special window is a promotion
      (the verdict is about L = lambda W: an arrivals counter can compensate)."""
    if "error" in out:
        return False, "error: " + str(out["error"])[:120]
    exp = truth["expect"]
    kind, verdict = exp["classification"], out["verdict"]
    cls = out["classification"]
    trans, prom = cls.get("transient", []), cls.get("promoted", [])
    growing = bool((out["total"].get("growing") or {}).get("growing"))
    if kind == "none":
        ok = verdict == "consistent" and not cls.get("systematic") and not trans and not prom
        return ok, "" if ok else f"{verdict}, {len(trans)} transient, {len(prom)} promoted"
    if kind == "systematic":
        odd = [t for t in trans if t["direction"] != exp["verdict"] or t["phase"] == "drain"]
        ok = verdict == exp["verdict"] and bool(cls.get("systematic")) and not odd and not prom
        return ok, "" if ok else f"{verdict}, {len(odd)} odd transient, {len(prom)} promoted"
    if kind == "growing":
        ok = verdict == exp["verdict"] and growing
        return ok, "" if ok else f"{verdict}, growing={growing}"
    [f] = truth["faults"]
    special = [t for t in trans if t["source"] == "special_cause"] + [
        {**p, "phase": "peak", "promoted_window": True} for p in prom
    ]
    spans = [(_ms(t["window"][0]), _ms(t["window"][1])) for t in trans + prom]
    win = max((b - a for a, b in spans), default=0)
    lo, load_end = f["start_unix_ms"], f["end_unix_ms"]
    hi = truth["started_unix_ms"] + int(f["effect"]["drain_end_s"] * 1000)
    inside = lambda t: _ms(t["window"][0]) < hi and _ms(t["window"][1]) > lo
    at_episode = [t for t in special if inside(t)]
    stray = [
        t
        for t in trans + prom
        if not (lo - win < _ms(t["window"][1]) and _ms(t["window"][0]) < hi + win)
    ]
    bad_phase = [
        t for t in special
        if (t["phase"] == "peak" and not (_ms(t["window"][0]) < load_end and _ms(t["window"][1]) > lo))
        or (t["phase"] == "drain" and _ms(t["window"][1]) <= lo)
    ]  # fmt: skip
    v_ok = verdict == exp["verdict"] or (
        verdict == "consistent" and any(t.get("promoted_window") for t in at_episode)
    )
    ok = v_ok and bool(at_episode) and not stray and not bad_phase
    why = (
        f"{verdict}; special at the episode: {len(at_episode)}; outside: {len(stray)}; "
        f"mislabelled: {len(bad_phase)}"
    )
    return ok, "" if ok else why


def coverage(truth: dict, out: dict) -> str:
    """Windows whose 95% measurement interval holds the exact R of the simulation (the realised
    path, queue_sim.exact_windows): the interval's own validation, whatever the verdict."""
    if "error" in out:
        return "-"
    rows = [w for w in out["total"]["windows"] if w[2] is not None]
    if len(out["total"]["windows"]) < 2 or not rows:
        return "-"
    cols = out["total"]["window_columns"]
    t0 = truth["started_unix_ms"]
    span = _ms(out["total"]["windows"][1][0]) - _ms(out["total"]["windows"][0][0])
    spans = [((_ms(w[0]) - t0) / 1000, min((_ms(w[0]) + span - t0) / 1000, truth["duration_s"]))
             for w in rows]  # fmt: skip
    exact = exact_windows(load_scenario(resolve_scenario(truth["scenario"])), spans)
    lo, hi = cols.index("ci95_lo"), cols.index("ci95_hi")
    k = sum(w[lo] <= e["R"] <= w[hi] for w, e in zip(rows, exact, strict=True))
    return f"{k}/{len(rows)}"


def row(out: dict) -> str:
    """One line per check: verdict, systematic level, growing, transient windows."""
    if "error" in out:
        return "error"
    cls = out["classification"]
    sysd = cls.get("systematic")
    bits = [out["verdict"]]
    if sysd:
        bits.append(f"sys {sysd['ratio']:.3g} [{sysd['ci95'][0]:.3g}, {sysd['ci95'][1]:.3g}]")
    if (out["total"].get("growing") or {}).get("growing"):
        bits.append("growing")
    for t in cls.get("transient", []):
        hhmm = t["window"][0][11:16]
        tag = "promoted " if t.get("promoted") else ""
        grid = " (shifted grid)" if t.get("grid") else ""
        bits.append(f"{hhmm} {t['ratio']:.3g} {t['phase']}/{tag}{t['source']}{grid}")
    seen = {t["window"][0] for t in cls.get("transient", [])}
    for p in cls.get("promoted", []):
        if p["window"][0] not in seen:
            grid = " (shifted grid)" if p.get("grid") else ""
            bits.append(
                f"{p['window'][0][11:16]} {p['ratio']:.3g} peak/promoted from {p['from']}{grid}"
            )
    return "; ".join(bits)


async def connect(mcp_url: str, vm_url: str, scrape_s: int) -> None:
    async with Client(mcp_url) as client:
        args = {"name": "vm", "url": vm_url, "flavor": "victoriametrics"}
        await client.call_tool("source_connect", {**args, "resolution": f"{scrape_s}s"})


async def check(mcp_url: str, truth: dict, warmup_s: int, window: str) -> tuple[dict, str]:
    sc = truth["scenario"]
    sel = lambda m: f'{m}{{scenario="{sc}"}}'
    start = truth["started_unix_ms"] + warmup_s * 1000
    end = truth["started_unix_ms"] + int(truth["duration_s"] * 1000)
    args = {
        "arrival_rate": sel(METRIC_REQUESTS), "latency": sel(METRIC_LATENCY),
        "concurrency": sel(METRIC_ACTIVE), "by": ["pod"],
        "start": str(start), "end": str(end), "source": "vm", "window": window,
    }  # fmt: skip
    async with Client(mcp_url) as client:
        res = await client.call_tool("check_littles_law", args)
    text = "\n".join(c.text for c in res.content if isinstance(c, TextContent))
    if res.is_error:
        return {"error": text}, text
    return json.loads(text), text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenarios", nargs="*", default=DEFAULT)
    ap.add_argument("--vm-port", type=int, default=8431)
    ap.add_argument("--host-alias", default="host.containers.internal")
    ap.add_argument(
        "--exporters",
        choices=("container", "host"),
        default="container",
        help="run the exporters in a container on the VM's network (default: the podman "
        "host-gateway path stalls scrapes for seconds) or on the host",
    )
    ap.add_argument("--duration", type=float, help="override duration_s (shorter runs)")
    ap.add_argument("--warmup", type=int, default=20, help="seconds of range start to skip")
    ap.add_argument("--windows", default="1m,2m,5m", help="comma list of check windows to evaluate")
    ap.add_argument("--tag", default="", help="suffix of the results file (results<tag>.json)")
    ap.add_argument("--keep", action="store_true", help="leave VM and daemon running afterwards")
    ap.add_argument("--analyse-only", action="store_true", help="reuse running VM + truth files")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    procs: list[subprocess.Popen] = []
    tmp = Path(tempfile.mkdtemp(prefix="tn-qs-"))
    scs = {n: load_scenario(resolve_scenario(n)) for n in a.scenarios}
    ports = (
        {n: 9301 + i for i, n in enumerate(scs)} if a.exporters == "container"
        else {n: free_port() for n in scs}
    )  # fmt: skip
    scrape_s = int(min(s.scrape_interval_s for s in scs.values()))
    try:
        if not a.analyse_only:
            for n in scs:
                (OUT / f"{n}.truth.json").unlink(missing_ok=True)
            runs = {}
            for n, sc in scs.items():
                arg = n
                if a.duration:
                    from dataclasses import asdict

                    d = asdict(sc)
                    d["duration_s"] = a.duration
                    d["faults"] = [{**f, "instances": list(f["instances"])} for f in d["faults"]]
                    d["instances"] = list(d["instances"])
                    (OUT / f"{n}.scenario.json").write_text(json.dumps(d))
                    arg = f"{n}.scenario.json"
                runs[n] = arg
            if a.exporters == "container":
                ip = start_exporters(a.vm_port, runs, ports)
                targets = {n: f"{ip}:{ports[n]}" for n in scs}
            else:
                for n, arg in runs.items():
                    src = str(OUT / arg) if arg.endswith(".json") else arg
                    cmd = [sys.executable, str(ROOT / "scripts" / "queue_sim.py"), src, "--port",
                           str(ports[n]), "--truth", str(OUT / f"{n}.truth.json")]  # fmt: skip
                    procs.append(subprocess.Popen(cmd, cwd=ROOT))
                targets = {n: f"{a.host_alias}:{ports[n]}" for n in scs}
            scrape = tmp / "scrape.yml"
            jobs = "".join(
                f"  - job_name: qs-{n}\n    scrape_interval: {scrape_s}s\n    static_configs:\n"
                f"      - targets: ['{targets[n]}']\n        labels: {{scenario: {n}}}\n"
                for n in scs
            )
            scrape.write_text("scrape_configs:\n" + jobs)
            start_vm(a.vm_port, scrape, scrape_s, network=a.exporters == "container")
            horizon = max((a.duration or s.duration_s) for s in scs.values())
            print(f"VM on :{a.vm_port}; scenarios run for {horizon:.0f}s ...", flush=True)
            wait_finished(scs, horizon + 120)
            time.sleep(2 * scrape_s + 3)
            for p in procs:
                p.wait(timeout=30)
            for n in scs:
                print(f"scrapes {n}: {scrape_health(a.vm_port, n, scrape_s)}", flush=True)
        port = free_port()
        daemon = subprocess.Popen(
            [sys.executable, "-m", "telemetry_nerd.cli", "serve", "--port", str(port),
             "--data-dir", str(tmp / "data"), "--source-url", f"http://127.0.0.1:{a.vm_port}"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        procs.append(daemon)
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code < 500:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        results = {}
        asyncio.run(
            connect(f"http://127.0.0.1:{port}/mcp", f"http://127.0.0.1:{a.vm_port}", scrape_s)
        )
        for n in scs:
            truth = json.loads((OUT / f"{n}.truth.json").read_text())
            for w in a.windows.split(","):
                out, text = asyncio.run(check(f"http://127.0.0.1:{port}/mcp", truth, a.warmup, w))
                (OUT / f"{n}.{w}.check.json").write_text(text)
                key = f"{n}@{w}"
                results[key] = analyse(truth, out) if "error" not in out else out
                ok, why = matches(truth, out)
                results[key]["match"], results[key]["row"] = ok, row(out)
                results[key]["coverage"] = coverage(truth, out)
                print(
                    f"\n== {key}: expected {truth['expect']} -> {'OK' if ok else 'MISMATCH'} {why}"
                )
                print(str(out.get("summary", out))[:700])
        (OUT / f"results{a.tag}.json").write_text(json.dumps(results, indent=2, default=str))
        print("\n| scenario | window | match | exact R in interval | result |")
        print("|---|---|---|---|---|")
        for key, r in results.items():
            n, w = key.split("@")
            ok = "yes" if r.get("match") else "NO"
            print(f"| {n} | {w} | {ok} | {r.get('coverage')} | {r.get('row')} |")
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        if not a.keep:
            stop_vm(a.vm_port)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
