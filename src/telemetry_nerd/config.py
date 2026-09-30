from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_UI = Path(__file__).resolve().parents[2] / "ui" / "dist"


@dataclass
class Settings:
    data_dir: Path = Path(".tn-data")
    source_url: str = "http://127.0.0.1:8428"
    source_flavor: str = "victoriametrics"
    resolution_ms: int = 15_000
    host: str = "127.0.0.1"
    port: int = 7070
    ui_dir: Path | None = field(default=_REPO_UI)

    @classmethod
    def from_env(cls) -> Settings:
        s = cls()
        s.data_dir = Path(os.environ.get("TN_DATA_DIR", s.data_dir))
        s.source_url = os.environ.get("TN_SOURCE_URL", s.source_url)
        s.source_flavor = os.environ.get("TN_SOURCE_FLAVOR", s.source_flavor)
        raw_port = os.environ.get("TN_PORT")
        if raw_port is not None:
            try:
                s.port = int(raw_port)
            except ValueError as e:
                raise ValueError(f"TN_PORT must be an integer, got {raw_port!r}") from e
        return s

    @property
    def ui_url(self) -> str:
        return f"http://{self.host}:{self.port}"
