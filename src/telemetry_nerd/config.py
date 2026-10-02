from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def default_data_dir() -> Path:
    if env := os.environ.get("TN_DATA_DIR"):
        return Path(env)
    if xdg := os.environ.get("XDG_DATA_HOME"):
        return Path(xdg) / "telemetry-nerd"
    return Path.home() / ".local" / "share" / "telemetry-nerd"


# The UI bundled into the wheel by hatch_build.py, else the repo checkout's ui/dist.
_BUNDLED_UI = Path(__file__).resolve().parent / "ui_dist"
_REPO_UI = Path(__file__).resolve().parents[2] / "ui" / "dist"


def default_ui_dir() -> Path:
    return _BUNDLED_UI if (_BUNDLED_UI / "index.html").exists() else _REPO_UI


@dataclass
class Settings:
    data_dir: Path = Path(".tn-data")
    source_url: str = "http://127.0.0.1:8428"
    source_flavor: str = "victoriametrics"
    resolution_ms: int = 15_000
    host: str = "127.0.0.1"
    port: int = 7070
    ui_dir: Path | None = field(default_factory=default_ui_dir)
    allowed_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_HOSTS))

    @classmethod
    def from_env(cls) -> Settings:
        s = cls()
        s.data_dir = default_data_dir()
        s.source_url = os.environ.get("TN_SOURCE_URL", s.source_url)
        s.source_flavor = os.environ.get("TN_SOURCE_FLAVOR", s.source_flavor)
        s.host = os.environ.get("TN_HOST", s.host)
        if extra := os.environ.get("TN_ALLOWED_HOSTS"):
            s.allowed_hosts += [h.strip() for h in extra.split(",") if h.strip()]
        raw_port = os.environ.get("TN_PORT")
        if raw_port is not None:
            try:
                s.port = int(raw_port)
            except ValueError as e:
                raise ValueError(f"TN_PORT must be an integer, got {raw_port!r}") from e
        return s

    @property
    def ui_url(self) -> str:
        # A wildcard bind (container) is not a connectable address: advertise loopback.
        host = {"0.0.0.0": "127.0.0.1", "::": "[::1]"}.get(self.host, self.host)
        return f"http://{host}:{self.port}"

    @property
    def daemon_url(self) -> str:
        return self.ui_url
