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
