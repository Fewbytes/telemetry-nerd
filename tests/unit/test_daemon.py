import socket
import subprocess
import threading
import time

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


def test_ensure_daemon_concurrent_calls_spawn_only_once(tmp_path, monkeypatch):
    """Regression for the check-then-spawn race (telemetry-nerd-au99): N concurrent
    ensure_daemon() calls against a fresh data_dir must spawn exactly one daemon."""
    settings = Settings(data_dir=tmp_path, port=7998)
    spawn_count = 0
    spawn_lock = threading.Lock()
    started = threading.Event()

    def fake_popen(cmd, **kw):
        nonlocal spawn_count
        with spawn_lock:
            spawn_count += 1

        def _become_healthy():
            time.sleep(0.1)
            daemon.write_state(tmp_path, settings.daemon_url, 12345)
            started.set()

        threading.Thread(target=_become_healthy, daemon=True).start()

        class P:
            pass

        return P()

    def fake_healthy(url, timeout=1.0):
        return started.is_set() and url == settings.daemon_url

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "healthy", fake_healthy)

    results: list[str] = []
    errors: list[Exception] = []

    def worker():
        try:
            results.append(daemon.ensure_daemon(settings, wait_s=5.0))
        except Exception as e:  # noqa: BLE001 - captured for assertion in the main thread
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert spawn_count == 1, f"expected exactly one spawn, got {spawn_count}"
    assert all(r == settings.daemon_url for r in results)
