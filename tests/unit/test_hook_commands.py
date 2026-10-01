"""Hook commands: `ensure` (SessionStart) and `pending` (UserPromptSubmit) fallback.

Both must exit 0 and stay silent unless they have something to deliver — hook
stdout becomes session context, and a failing hook must never block the session.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest
from websockets.sync.client import connect

from telemetry_nerd import daemon
from telemetry_nerd.cli import main


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def hook_env(tmp_path, monkeypatch, live_daemon):
    """Point the CLI settings env at the live daemon via the daemon state file."""
    monkeypatch.setenv("TN_DATA_DIR", str(tmp_path))
    daemon.write_state(tmp_path, live_daemon.url, pid=0)
    return tmp_path


def _ask(url: str) -> None:
    r = httpx.post(f"{url}/api/threads", json={"text": "why the dip?"})
    assert r.status_code == 200


def test_pending_prints_wrapped_block_once(hook_env, live_daemon, capsys):
    _ask(live_daemon.url)
    assert main(["pending"]) is None
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith('<telemetry-nerd-ui-events seqs="') and lines[0].endswith('">')
    assert lines[1] == 'user asked in t1: "why the dip?"'
    assert lines[2] == "</telemetry-nerd-ui-events>"
    # The claim advanced the cursor: a second run delivers nothing.
    main(["pending"])
    assert capsys.readouterr().out == ""


def test_pending_silent_while_channel_bridge_live(hook_env, live_daemon, capsys):
    _ask(live_daemon.url)
    ws_url = live_daemon.url.replace("http://", "ws://") + "/ws/bridge?consumer=claude"
    with connect(ws_url) as bridge:
        bridge.send(json.dumps({"type": "hello", "mode": "channel"}))
        bridge.send(json.dumps({"type": "ready"}))
        assert json.loads(bridge.recv(timeout=5))["type"] == "deliver"
        main(["pending"])
        assert capsys.readouterr().out == ""


def test_pending_delivers_while_bridge_in_hook_mode(hook_env, live_daemon, capsys):
    _ask(live_daemon.url)
    ws_url = live_daemon.url.replace("http://", "ws://") + "/ws/bridge?consumer=claude"
    with connect(ws_url) as bridge:
        bridge.send(json.dumps({"type": "hello", "mode": "hook"}))
        bridge.send(json.dumps({"type": "ready"}))
        main(["pending"])
        assert "why the dip?" in capsys.readouterr().out


def test_pending_silent_when_daemon_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TN_PORT", str(_closed_port()))
    daemon.write_state(tmp_path, f"http://127.0.0.1:{_closed_port()}", pid=0)
    main(["pending"])
    assert capsys.readouterr().out == ""


def test_ensure_prints_url_line(hook_env, live_daemon, capsys):
    main(["ensure"])
    assert capsys.readouterr().out == f"Telemetry Nerd workspace: {live_daemon.url}\n"


def test_ensure_failure_prints_reason_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TN_DATA_DIR", str(tmp_path))

    def boom(settings):
        raise RuntimeError("daemon did not become healthy at http://127.0.0.1:7070")

    monkeypatch.setattr(daemon, "ensure_daemon", boom)
    assert main(["ensure"]) is None  # never blocks the session with a non-zero exit
    assert capsys.readouterr().out == (
        "Telemetry Nerd daemon not running: daemon did not become healthy at"
        " http://127.0.0.1:7070\n"
    )
