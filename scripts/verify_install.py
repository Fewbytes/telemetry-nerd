#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Clean-machine install check (bead ijg.3).

Builds the wheel (or takes --wheel), asserts it bundles the UI and ships no dev-only deps,
installs it with `uv tool install` into throwaway tool dirs, runs `telemetry-nerd --help`,
starts `serve` on a free port and fetches the UI index plus an API route.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from email.parser import Parser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEV_ONLY = {"pytest", "hypothesis", "respx", "ruff", "testcontainers", "pytest-asyncio"}


def check_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        meta = next(n for n in names if n.endswith(".dist-info/METADATA"))
        requires = Parser().parsestr(z.read(meta).decode()).get_all("Requires-Dist") or []
    assert "telemetry_nerd/ui_dist/index.html" in names, "wheel lacks bundled UI index.html"
    assets = [n for n in names if n.startswith("telemetry_nerd/ui_dist/assets/")]
    assert any(n.endswith(".js") for n in assets), "wheel lacks UI JS bundle"
    bad = {r.split()[0].split(";")[0].split(">")[0].split("=")[0] for r in requires} & DEV_ONLY
    assert not bad, f"dev-only deps leak into runtime requirements: {bad}"
    print(f"wheel ok: {wheel.name} ({len(assets)} UI assets)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel", type=Path)
    ap.add_argument("--skip-install", action="store_true", help="only check wheel contents")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="tn-verify-") as tmp:
        tmp_p = Path(tmp)
        wheel = args.wheel
        if wheel is None:
            out = tmp_p / "dist"
            subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out), str(ROOT)], check=True)
            wheel = next(out.glob("*.whl"))
        check_wheel(wheel)
        if args.skip_install:
            return
        env = {
            **os.environ,
            "UV_TOOL_DIR": str(tmp_p / "tools"),
            "UV_TOOL_BIN_DIR": str(tmp_p / "bin"),
            "TN_DATA_DIR": str(tmp_p / "data"),
        }
        env.pop("VIRTUAL_ENV", None)
        subprocess.run(
            ["uv", "tool", "install", "--from", str(wheel), "telemetry-nerd"], check=True, env=env
        )
        exe = tmp_p / "bin" / "telemetry-nerd"
        subprocess.run([str(exe), "--help"], check=True, env=env, stdout=subprocess.DEVNULL)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen(
            [str(exe), "serve", "--port", str(port)], env=env, stderr=subprocess.PIPE, text=True
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(60):
                try:
                    if httpx.get(f"{base}/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.5)
            else:
                raise SystemExit("daemon did not become healthy")
            idx = httpx.get(f"{base}/")
            assert idx.status_code == 200 and "<div id=" in idx.text, "UI index not served"
            api = httpx.get(f"{base}/api/panels")
            assert api.status_code == 200, f"/api/panels -> {api.status_code}"
            print(f"serve ok: / -> {idx.status_code}, /api/panels -> {api.status_code}")
        finally:
            proc.terminate()
            proc.wait(10)
    print("install verification passed")


if __name__ == "__main__":
    sys.exit(main())
