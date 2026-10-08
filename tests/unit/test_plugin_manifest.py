"""Plugin/marketplace metadata stays in sync with pyproject, and the launcher is wired up."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCH = ROOT / "scripts" / "tn-launch"

_spec = importlib.util.spec_from_file_location("check_version", ROOT / "scripts/check_version.py")
check_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version)


def test_versions_in_sync():
    assert check_version.check() == []


def test_tag_must_match_version():
    version = check_version.versions()["pyproject.toml"]
    assert check_version.check(f"v{version}") == []
    assert check_version.check("v999.0.0")


def test_plugin_metadata_complete():
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    for key in ("name", "version", "description", "homepage", "repository", "keywords"):
        assert plugin[key]
    assert plugin["repository"] == "https://github.com/Fewbytes/telemetry-nerd"
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert [p["name"] for p in market["plugins"]] == [plugin["name"]]
    assert market["plugins"][0]["source"] == "./"


def test_mcp_and_hooks_use_launcher():
    # Installed / --plugin-dir: the manifest's mcpServers (it replaces a same-named server from the
    # root .mcp.json) uses plain ${CLAUDE_PLUGIN_ROOT}, which Claude Code substitutes. The
    # shell-default form `${CLAUDE_PLUGIN_ROOT:-.}` is not substituted for plugins and resolves to
    # the session cwd (eval finding 981).
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    pmcp = plugin["mcpServers"]["telemetry-nerd"]
    assert pmcp["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/tn-launch", "bridge"]
    # Dev (this repo as the project, no plugin): the project-level .mcp.json has no
    # CLAUDE_PLUGIN_ROOT, so it falls back to the cwd (the repo root).
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["telemetry-nerd"]
    assert mcp["args"] == ["${CLAUDE_PLUGIN_ROOT:-.}/scripts/tn-launch", "bridge"]
    assert pmcp["command"] == mcp["command"] and pmcp["env"] == mcp["env"]
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
    cmds = [h["command"] for ev in hooks.values() for g in ev for h in g["hooks"]]
    assert cmds and all("scripts/tn-launch" in c for c in cmds)
    assert LAUNCH.stat().st_mode & 0o111


def _run(*args: str, path: str, root: Path = ROOT):
    env = {"PATH": path, "CLAUDE_PLUGIN_ROOT": str(root)}
    return subprocess.run(
        [shutil.which("sh"), str(LAUNCH), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def fake_bin(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    tn = b / "telemetry-nerd"
    tn.write_text('#!/bin/sh\necho "installed: $*"\n')
    tn.chmod(0o755)
    return b


def test_launcher_prefers_path_install(fake_bin):
    r = _run("bridge", path=f"{fake_bin}:/usr/bin:/bin")
    assert r.returncode == 0 and r.stdout == "installed: bridge\n"


def test_launcher_fails_loudly_without_install(tmp_path):
    r = _run("bridge", path="/usr/bin:/bin", root=tmp_path)
    assert r.returncode == 127
    assert r.stdout == ""  # stdout is the MCP channel
    assert "uv tool install" in r.stderr


def test_launcher_ensure_hook_never_fails(tmp_path):
    r = _run("ensure", path="/usr/bin:/bin", root=tmp_path)
    assert r.returncode == 0 and "uv tool install" in r.stdout


def test_launcher_falls_back_to_source_checkout(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    (b / "uv").write_text('#!/bin/sh\necho "uv $*"\n')
    (b / "uv").chmod(0o755)
    r = _run("bridge", path=f"{b}:/usr/bin:/bin")
    assert r.stdout == f"uv run --directory {ROOT} telemetry-nerd bridge\n"


@pytest.fixture
def fake_runtime(tmp_path):
    """A fake `podman` (or `docker`) that just echoes its argv, like fake_bin for telemetry-nerd."""

    def _make(name):
        b = tmp_path / "bin"
        b.mkdir(exist_ok=True)
        rt = b / name
        rt.write_text('#!/bin/sh\necho "$0 $*"\n')
        rt.chmod(0o755)
        return b

    return _make


def test_launcher_falls_back_to_container_exec_bridge(fake_runtime, tmp_path):
    b = fake_runtime("podman")
    env = {
        "PATH": f"{b}:/usr/bin:/bin",
        "CLAUDE_PLUGIN_ROOT": str(tmp_path),  # no pyproject.toml here: no source checkout
        "TN_CONTAINER": "telemetry-nerd",
    }
    r = subprocess.run(
        [shutil.which("sh"), str(LAUNCH), "bridge"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    assert (
        r.stdout
        == f"{b}/podman exec -i telemetry-nerd telemetry-nerd bridge --daemon-url http://127.0.0.1:7070\n"
    )


def test_launcher_container_exec_passes_through_non_bridge_commands(fake_runtime, tmp_path):
    b = fake_runtime("podman")
    env = {
        "PATH": f"{b}:/usr/bin:/bin",
        "CLAUDE_PLUGIN_ROOT": str(tmp_path),
        "TN_CONTAINER": "telemetry-nerd",
    }
    r = subprocess.run(
        [shutil.which("sh"), str(LAUNCH), "ensure"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    assert r.stdout == f"{b}/podman exec -i telemetry-nerd telemetry-nerd ensure\n"


def test_launcher_container_exec_respects_container_env_for_docker(fake_runtime, tmp_path):
    b = fake_runtime("docker")
    env = {
        "PATH": f"{b}:/usr/bin:/bin",
        "CLAUDE_PLUGIN_ROOT": str(tmp_path),
        "TN_CONTAINER": "telemetry-nerd",
        "CONTAINER": "docker",
    }
    r = subprocess.run(
        [shutil.which("sh"), str(LAUNCH), "bridge"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    assert "docker exec -i telemetry-nerd telemetry-nerd bridge" in r.stdout


def test_launcher_ignores_tn_container_without_runtime(tmp_path):
    # TN_CONTAINER set but neither podman nor docker on PATH: falls through to the normal
    # fail-loudly path, not a crash. Uses an empty dir rather than the real /usr/bin:/bin --
    # some CI runner images ship podman there, which would find a real runtime and take the
    # TN_CONTAINER branch instead of the fail-loudly one this test checks.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {
        "PATH": str(empty_bin),
        "CLAUDE_PLUGIN_ROOT": str(tmp_path),
        "TN_CONTAINER": "telemetry-nerd",
    }
    r = subprocess.run(
        [shutil.which("sh"), str(LAUNCH), "bridge"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 127
    assert "uv tool install" in r.stderr
