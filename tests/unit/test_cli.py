import logging
import sys

import anyio
import duckdb
import pytest

from telemetry_nerd import cli


def test_configure_logging_goes_to_stderr_and_quiets_httpx():
    cli._configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    handlers = logging.getLogger().handlers
    assert handlers
    assert any(getattr(h, "stream", None) is sys.stderr for h in handlers)


def test_uvicorn_access_log_is_disabled(tmp_path):
    settings = cli.Settings.from_env()
    settings.data_dir = tmp_path
    config = cli._uvicorn_config(lambda *a: None, settings)  # type: ignore[arg-type]
    assert config.access_log is False


def test_allowed_host_flag_extends_defaults():
    from telemetry_nerd import cli

    args = cli._parse(["serve", "--allowed-host", "nerd.lan", "--allowed-host", "x"])
    hosts = cli._settings(args).allowed_hosts
    assert hosts[-2:] == ["nerd.lan", "x"]
    assert "127.0.0.1" in hosts


def test_no_mcp_flag_is_accepted_noop():
    args = cli._parse(["serve", "--no-mcp"])
    assert args.command == "serve"


def test_default_data_dir_env_precedence(monkeypatch, tmp_path):
    from pathlib import Path

    from telemetry_nerd.config import Settings

    monkeypatch.delenv("TN_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert Settings.from_env().data_dir == tmp_path / "telemetry-nerd"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Settings.from_env().data_dir == Path.home() / ".local/share/telemetry-nerd"
    monkeypatch.setenv("TN_DATA_DIR", "/x/y")
    assert Settings.from_env().data_dir == Path("/x/y")
    assert Settings.from_env().daemon_url == "http://127.0.0.1:7070"


def test_serve_exits_quietly_on_duckdb_lock_conflict(tmp_path, monkeypatch, caplog):
    """telemetry-nerd-au99 layer 2: a losing daemon that still hits the DuckDB lock
    (e.g. started directly, bypassing ensure_daemon's flock) logs one line and returns
    instead of raising a traceback."""

    def boom(settings):
        raise duckdb.IOException("Could not set lock on file")

    monkeypatch.setattr(cli, "build_service", boom)
    settings = cli.Settings.from_env()
    settings.data_dir = tmp_path

    with caplog.at_level(logging.INFO, logger="telemetry_nerd.cli"):
        anyio.run(cli._serve, settings)

    assert any("already holds the lock" in r.message for r in caplog.records)


def test_serve_refuses_when_healthy_daemon_same_data_dir(tmp_path, monkeypatch, capsys):
    import pytest

    from telemetry_nerd import daemon

    daemon.write_state(tmp_path, "http://127.0.0.1:7070", 1)
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: True)
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--data-dir", str(tmp_path)])
    assert e.value.code == 1
    assert "already running" in capsys.readouterr().err


def test_bridge_uses_tn_daemon_url_and_never_spawns(monkeypatch):
    import argparse

    from telemetry_nerd import daemon
    from telemetry_nerd.config import Settings

    monkeypatch.setenv("TN_DAEMON_URL", "http://127.0.0.1:7070")
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: True)

    def no_spawn(settings):
        raise AssertionError("must not spawn")

    monkeypatch.setattr(daemon, "ensure_daemon", no_spawn)
    args = argparse.Namespace(daemon_url=None, no_autostart=False)
    url = cli._bridge_url(args, Settings(), logging.getLogger("t"))
    assert url == "http://127.0.0.1:7070"


def test_bridge_tn_daemon_url_unhealthy_exits_with_hint(monkeypatch, caplog):
    import argparse

    import pytest

    from telemetry_nerd import daemon
    from telemetry_nerd.config import Settings

    monkeypatch.setenv("TN_DAEMON_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: False)
    args = argparse.Namespace(daemon_url=None, no_autostart=False)
    with pytest.raises(SystemExit) as e:
        cli._bridge_url(args, Settings(), logging.getLogger("t"))
    assert e.value.code == 1
    assert "daemon not healthy at http://127.0.0.1:1" in caplog.text


def test_ensure_with_tn_daemon_url_does_not_spawn(monkeypatch, capsys):
    from telemetry_nerd import daemon

    # port 1: nothing listens there, so no live daemon can answer even if a stub is missed
    monkeypatch.setenv("TN_DAEMON_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: True)
    monkeypatch.setattr(daemon, "ensure_daemon", lambda s: (_ for _ in ()).throw(AssertionError))
    # and never ask a daemon for its proposals summary (y7hb: no live dependencies)
    monkeypatch.setattr(cli, "_retro_lines", lambda url: [])
    cli.main(["ensure"])
    assert capsys.readouterr().out == "Telemetry Nerd workspace: http://127.0.0.1:1\n"
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: False)
    cli.main(["ensure"])
    assert "no healthy daemon at TN_DAEMON_URL=http://127.0.0.1:1" in capsys.readouterr().out


def _keepalive_settings(tmp_path):
    settings = cli.Settings.from_env()
    settings.data_dir = tmp_path
    return settings


def test_uvicorn_keepalive_outlives_client_idle_pools(tmp_path):
    """3szb: the server must not be the side that closes an idle keep-alive connection.

    Node's agent (Playwright's request context) and httpx keep idle sockets for 5s, uvicorn's
    default; a request sent on a pooled socket just as the server closes it gets ECONNRESET.
    """
    config = cli._uvicorn_config(lambda *a: None, _keepalive_settings(tmp_path))  # type: ignore[arg-type]
    assert config.timeout_keep_alive >= 30


@pytest.mark.slow
def test_idle_keepalive_connection_is_still_served_after_six_seconds(tmp_path):
    import http.client
    import socket
    import threading
    import time

    import uvicorn

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(cli._uvicorn_config(app, _keepalive_settings(tmp_path)))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        assert conn.getresponse().read() == b"ok"
        time.sleep(6)  # idle past uvicorn's 5s default, as yview's request context did on CI
        conn.request("GET", "/")  # same socket: fails if the server closed it meanwhile
        assert conn.getresponse().read() == b"ok"
        conn.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
