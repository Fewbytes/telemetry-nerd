import socket
import subprocess

from telemetry_nerd import daemon
from telemetry_nerd.config import Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_state_roundtrip_and_remove(tmp_path):
    assert daemon.read_state(tmp_path) is None
    daemon.write_state(tmp_path, "http://127.0.0.1:1234", 42)
    assert daemon.state_path(tmp_path).name == "daemon.json"
    assert daemon.read_state(tmp_path) == {"url": "http://127.0.0.1:1234", "pid": 42}
    daemon.remove_state(tmp_path)
    assert daemon.read_state(tmp_path) is None
    daemon.remove_state(tmp_path)  # idempotent


def test_read_state_ignores_corrupt_file(tmp_path):
    daemon.state_path(tmp_path).write_text("{nope")
    assert daemon.read_state(tmp_path) is None


def test_healthy_false_on_unused_port():
    assert daemon.healthy(f"http://127.0.0.1:{_free_port()}", timeout=0.2) is False


def test_ensure_daemon_reuses_healthy_daemon(tmp_path, monkeypatch):
    daemon.write_state(tmp_path, "http://127.0.0.1:9", 1)
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: True)

    def boom(*a, **k):
        raise AssertionError("must not spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    settings = Settings(data_dir=tmp_path)
    assert daemon.ensure_daemon(settings) == "http://127.0.0.1:9"


def test_ensure_daemon_spawns_when_unhealthy(tmp_path, monkeypatch):
    calls = []
    states = iter([False, False, True])
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: next(states))

    class P:
        pass

    def fake_popen(cmd, **kw):
        calls.append((cmd, kw))
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    settings = Settings(data_dir=tmp_path, port=7999)
    url = daemon.ensure_daemon(settings, wait_s=5.0)
    assert url == "http://127.0.0.1:7999"
    cmd, kw = calls[0]
    assert "telemetry_nerd.cli" in cmd and "serve" in cmd
    assert kw["start_new_session"] is True
