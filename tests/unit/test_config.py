from pathlib import Path

from telemetry_nerd import config
from telemetry_nerd.config import Settings


def test_ui_dir_prefers_bundled_wheel_assets(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("x")
    monkeypatch.setattr(config, "_BUNDLED_UI", tmp_path)
    assert Settings().ui_dir == tmp_path


def test_ui_dir_falls_back_to_repo_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_BUNDLED_UI", tmp_path / "missing")
    assert Settings().ui_dir == config._REPO_UI


def test_host_from_env_and_wildcard_bind_advertises_loopback(monkeypatch):
    monkeypatch.setenv("TN_HOST", "0.0.0.0")
    s = Settings.from_env()
    assert s.host == "0.0.0.0"
    assert s.ui_url == "http://127.0.0.1:7070"
    assert isinstance(s.data_dir, Path)
