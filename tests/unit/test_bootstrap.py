from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service


def test_build_service_creates_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    svc = build_service(settings)
    assert (tmp_path / "data" / "series.duckdb").exists()
    assert (tmp_path / "data" / "workspace.db").exists()
    assert set(svc.sources) == {"default"}
    assert svc.sources["default"].name == "default"


async def test_runtime_sources_survive_rebuild(tmp_path, monkeypatch):
    from telemetry_nerd.sources.spec import SourceSpec

    settings = Settings(data_dir=tmp_path / "data")
    svc = build_service(settings)

    async def ok(self) -> dict:
        return {"reachable": True}

    monkeypatch.setattr("telemetry_nerd.sources.promql.PromQLSource.probe", ok)
    await svc.source_connect(SourceSpec(name="play", url="https://play.test/prom"))
    rebuilt = build_service(settings)
    assert set(rebuilt.sources) == {"default", "play"}
    assert rebuilt.sources["play"].base_url == "https://play.test/prom"
