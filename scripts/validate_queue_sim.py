"""Validate check_littles_law against queue-sim ground truth on a REAL VictoriaMetrics (bead 317).

  uv run python scripts/validate_queue_sim.py [scenario ...] [--vm-port 8430] [--keep]

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
    load_scenario,
    resolve_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "queue-sim" / "validate"
VM_IMAGE = "victoriametrics/victoria-metrics:v1.137.0"
CONTAINER, VOLUME = "tn-qs-vm", "tn-qs-vmdata"
DEFAULT = ["consistent", "hidden_queueing", "missing_instance", "overload_spike", "leak"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def start_vm(port: int, scrape_yml: Path, scrape_s: int) -> None:
    sh("podman", "rm", "-f", CONTAINER, check=False)
    sh("podman", "volume", "rm", "-f", VOLUME, check=False)
    sh("podman", "run", "-d", "--name", CONTAINER, "-p", f"{port}:8428", "-v", f"{VOLUME}:/data",
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


def stop_vm() -> None:
    sh("podman", "rm", "-f", CONTAINER, check=False)
    sh("podman", "volume", "rm", "-f", VOLUME, check=False)


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
    ap.add_argument("--vm-port", type=int, default=8430)
    ap.add_argument("--host-alias", default="host.containers.internal")
    ap.add_argument("--duration", type=float, help="override duration_s (shorter runs)")
    ap.add_argument("--warmup", type=int, default=20, help="seconds of range start to skip")
    ap.add_argument("--windows", default="1m,2m", help="comma list of check windows to evaluate")
    ap.add_argument("--keep", action="store_true", help="leave VM and daemon running afterwards")
    ap.add_argument("--analyse-only", action="store_true", help="reuse running VM + truth files")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    procs: list[subprocess.Popen] = []
    tmp = Path(tempfile.mkdtemp(prefix="tn-qs-"))
    scs = {n: load_scenario(resolve_scenario(n)) for n in a.scenarios}
    ports = {n: free_port() for n in scs}
    scrape_s = int(min(s.scrape_interval_s for s in scs.values()))
    try:
        if not a.analyse_only:
            for n, sc in scs.items():
                cmd = [sys.executable, str(ROOT / "scripts" / "queue_sim.py"), n, "--port",
                       str(ports[n]), "--truth", str(OUT / f"{n}.truth.json")]  # fmt: skip
                if a.duration:
                    patched = tmp / f"{n}.json"
                    from dataclasses import asdict

                    d = asdict(sc)
                    d["duration_s"] = a.duration
                    d["faults"] = [{**f, "instances": list(f["instances"])} for f in d["faults"]]
                    d["instances"] = list(d["instances"])
                    patched.write_text(json.dumps(d))
                    cmd[2] = str(patched)
                procs.append(subprocess.Popen(cmd, cwd=ROOT))
            scrape = tmp / "scrape.yml"
            jobs = "".join(
                f"  - job_name: qs-{n}\n    scrape_interval: {scrape_s}s\n    static_configs:\n"
                f"      - targets: ['{a.host_alias}:{ports[n]}']\n        labels: {{scenario: {n}}}\n"
                for n in scs
            )
            scrape.write_text("scrape_configs:\n" + jobs)
            start_vm(a.vm_port, scrape, scrape_s)
            horizon = max((a.duration or s.duration_s) for s in scs.values())
            print(f"VM on :{a.vm_port}; scenarios run for {horizon:.0f}s ...", flush=True)
            time.sleep(horizon + 2 * scrape_s + 3)
            for p in procs:
                p.wait(timeout=30)
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
                print(f"\n== {key}: expected {truth['expect']} -> {results[key].get('verdict')}")
                print(str(out.get("summary", out))[:700])
        (OUT / "results.json").write_text(json.dumps(results, indent=2, default=str))
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        if not a.keep:
            stop_vm()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
