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
import json
import os
import select
import signal
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mcp_handshake(env: dict[str, str], label: str) -> int:
    """Run the plugin launcher's `bridge` (as .mcp.json does) and do initialize + tools/list.

    Returns the tool count. Proves the launcher resolves the installed CLI and the bridge
    speaks MCP over stdio."""
    proc = subprocess.Popen(
        ["sh", str(ROOT / "scripts" / "tn-launch"), "bridge"],
        env={**env, "CLAUDE_PLUGIN_ROOT": str(ROOT)},
        cwd=env["TN_DATA_DIR"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin and proc.stdout

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv(want_id: int, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("id") == want_id:
                    return msg
        raise SystemExit(f"{label}: no reply to id {want_id}; stderr: {_drain(proc)}")

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "verify-install", "version": "0"},
                },
            }
        )
        recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = recv(2)["result"]["tools"]
    finally:
        proc.terminate()
        proc.wait(10)
    assert tools, f"{label}: tools/list was empty"
    print(f"launcher handshake ok ({label}): {len(tools)} tools")
    return len(tools)


def _drain(proc: subprocess.Popen) -> str:
    proc.terminate()
    try:
        return (proc.communicate(timeout=10)[1] or "")[-800:]
    except subprocess.TimeoutExpired:
        return "<timeout>"


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
        # Plugin mode "uv tool": launcher finds telemetry-nerd on PATH and the bridge autostarts
        # a daemon from the installed tool (own port + data dir, killed afterwards).
        auto_port = free_port()
        auto_env = {
            **env,
            "PATH": f"{tmp_p / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "TN_PORT": str(auto_port),
            "TN_DATA_DIR": str(tmp_p / "auto-data"),
        }
        (tmp_p / "auto-data").mkdir()
        try:
            mcp_handshake(auto_env, "uv tool install + autostart")
        finally:
            state = tmp_p / "auto-data" / "daemon.json"
            if state.exists():
                try:
                    os.kill(json.loads(state.read_text())["pid"], signal.SIGTERM)
                except (OSError, ValueError, KeyError):
                    pass
        port = free_port()
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
            # Plugin mode "container daemon": TN_DAEMON_URL points at an already running daemon.
            mcp_handshake(
                {
                    **env,
                    "PATH": f"{tmp_p / 'bin'}{os.pathsep}{os.environ['PATH']}",
                    "TN_DAEMON_URL": base,
                },
                "TN_DAEMON_URL daemon",
            )
        finally:
            proc.terminate()
            proc.wait(10)
    print("install verification passed")


if __name__ == "__main__":
    sys.exit(main())
