from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service


def test_build_service_creates_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    svc = build_service(settings)
    assert (tmp_path / "data" / "series.duckdb").exists()
    assert (tmp_path / "data" / "workspace.db").exists()
    assert set(svc.sources) == {"default"}
    assert svc.sources["default"].name == "default"
