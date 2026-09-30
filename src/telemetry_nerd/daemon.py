"""Daemon lifecycle: state file, health probe, detached spawn."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from telemetry_nerd.config import DEFAULT_ALLOWED_HOSTS, Settings


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "daemon.json"


def write_state(data_dir: Path, url: str, pid: int) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"url": url, "pid": pid}))
    os.replace(tmp, path)


def read_state(data_dir: Path) -> dict | None:
    try:
        data = json.loads(state_path(data_dir).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "url" in data else None


def remove_state(data_dir: Path, pid: int | None = None) -> None:
    """Remove the state file (only if it belongs to `pid`, when given)."""
    if pid is not None:
        state = read_state(data_dir)
        if state is not None and state.get("pid") != pid:
            return
    state_path(data_dir).unlink(missing_ok=True)


def healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        r = httpx.get(f"{url}/api/health", timeout=timeout)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def ensure_daemon(settings: Settings, wait_s: float = 15.0) -> str:
    """Return the URL of a healthy daemon for this data dir, spawning one if needed."""
    state = read_state(settings.data_dir)
    if state is not None and healthy(state["url"]):
        return state["url"]
    url = settings.daemon_url
    if healthy(url):
        return url
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "telemetry_nerd.cli",
        "serve",
        "--data-dir",
        str(settings.data_dir),
        "--source-url",
        settings.source_url,
        "--source-flavor",
        settings.source_flavor,
        "--host",
        settings.host,
        "--port",
        str(settings.port),
    ]
    if settings.ui_dir is not None:
        cmd += ["--ui-dir", str(settings.ui_dir)]
    cmd += [f"--allowed-host={h}" for h in settings.allowed_hosts if h not in DEFAULT_ALLOWED_HOSTS]
    try:
        with open(settings.data_dir / "daemon.log", "ab") as log:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError as e:
        raise RuntimeError(f"cannot write daemon log in {settings.data_dir}: {e}") from e
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if healthy(url):
            return url
        time.sleep(0.2)
    raise RuntimeError(
        f"daemon did not become healthy at {url}; see {settings.data_dir}/daemon.log"
    )
