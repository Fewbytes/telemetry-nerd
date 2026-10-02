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
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["telemetry-nerd"]
    assert mcp["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/tn-launch", "bridge"]
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
