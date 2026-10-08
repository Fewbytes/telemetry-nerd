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


from telemetry_nerd.core.bootstrap import source_factory
from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.spec import SourceSpec


def test_the_factory_dispatches_on_flavor(tmp_path):
    es = source_factory(SourceSpec(name="logs", url="http://es:9200", flavor="opensearch",
                                   index_pattern="access-*", time_field="@timestamp"), tmp_path)  # fmt: skip
    assert isinstance(es, ElasticsearchSource) and es.flavor == "opensearch"
    prom = source_factory(
        SourceSpec(name="vm", url="http://vm:8428", flavor="victoriametrics"), tmp_path
    )
    assert isinstance(prom, PromQLSource)


def test_source_factory_threads_data_dir_to_oauth_sources(tmp_path):
    spec = SourceSpec.model_validate(
        {
            "name": "sso",
            "url": "http://prom.example.com",
            "auth": {
                "authorize_url": "https://idp.example.com/authorize",
                "token_url": "https://idp.example.com/token",
                "client_id": "tn-client",
            },
        }
    )
    source = source_factory(spec, tmp_path)
    assert source._token_provider is not None


async def test_runtime_es_sources_survive_rebuild(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    svc = build_service(settings)

    async def ok(self) -> dict:
        return {"reachable": True}

    monkeypatch.setattr(ElasticsearchSource, "probe", ok)
    await svc.source_connect(SourceSpec(name="logs", url="http://es:9200", flavor="elasticsearch",
                                        index_pattern="access-*", time_field="@timestamp"))  # fmt: skip
    rebuilt = build_service(settings)
    assert isinstance(rebuilt.sources["logs"], ElasticsearchSource)
