import logging
import sys

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


def test_serve_refuses_when_healthy_daemon_same_data_dir(tmp_path, monkeypatch, capsys):
    import pytest

    from telemetry_nerd import daemon

    daemon.write_state(tmp_path, "http://127.0.0.1:7070", 1)
    monkeypatch.setattr(daemon, "healthy", lambda url, timeout=1.0: True)
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--data-dir", str(tmp_path)])
    assert e.value.code == 1
    assert "already running" in capsys.readouterr().err
