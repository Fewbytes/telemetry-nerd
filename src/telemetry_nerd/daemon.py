"""Daemon lifecycle: state file, health probe, detached spawn."""

from __future__ import annotations

import fcntl
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


def lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / "daemon.lock"


def _acquire_lock_nb(lock_file, wait_s: float, poll_s: float = 0.05) -> bool:
    """Poll for the exclusive lock instead of blocking forever (telemetry-nerd-vrtd): a caller
    whose own process hangs while holding the lock (e.g. a wedged spawn) must not wedge every
    other ensure_daemon() caller along with it."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)


def ensure_daemon(settings: Settings, wait_s: float = 15.0) -> str:
    """Return the URL of a healthy daemon for this data dir, spawning one if needed.

    The whole check-then-spawn decision is made under an flock on
    `data_dir/daemon.lock` (telemetry-nerd-au99): two concurrent callers can both
    observe "not healthy", but only the one that wins the lock spawns a daemon. The
    other blocks on the lock and, once it acquires it, re-checks health (now true,
    since the winner's daemon has finished starting) and returns without spawning.

    The lock wait itself is bounded (telemetry-nerd-vrtd): if the holder doesn't release it
    within `wait_s`, we give up waiting and do one final health re-check (the holder's daemon
    may have come up fine even though it's still sitting on the lock) before failing.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_path(settings.data_dir), "a+") as lock_file:
        if not _acquire_lock_nb(lock_file, wait_s):
            state = read_state(settings.data_dir)
            if state is not None and healthy(state["url"]):
                return state["url"]
            if healthy(settings.daemon_url):
                return settings.daemon_url
            raise RuntimeError(
                f"timed out waiting for daemon.lock in {settings.data_dir} "
                "(the lock holder may be hung)"
            )
        try:
            return _check_then_spawn(settings, wait_s)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _check_then_spawn(settings: Settings, wait_s: float) -> str:
    state = read_state(settings.data_dir)
    if state is not None and healthy(state["url"]):
        return state["url"]
    url = settings.daemon_url
    if healthy(url):
        return url
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
