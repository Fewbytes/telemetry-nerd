import json

import pytest

from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from telemetry_nerd.sources.spec import AuthRef, SourceSpec
from tests.unit.fakes import FakeSource, make_service

URL = "https://play.test/prom"


class DownSource(FakeSource):
    async def probe(self) -> dict:
        raise SourceUnavailable("cannot reach", hint="check the URL")


async def test_connect_probes_persists_and_logs(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.source_connect(SourceSpec(name="play", url=URL))
    assert out["source"]["name"] == "play"
    assert out["status"]["reachable"] is True
    assert "play" in svc.sources
    [event] = [e for e in svc.log.since(0, 100) if e.type == "source.connected"]
    assert event.object_id == "play" and event.actor == "claude"


async def test_query_routes_to_named_source(tmp_path):
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="play", url=URL))
    await svc.query("up", "now-2h", "now-1h", source="play")
    assert svc.sources["play"].calls > 0  # the cache may fetch in several chunks
    assert svc.sources["default"].calls == 0


async def test_unreachable_source_is_not_added(tmp_path):
    svc = make_service(tmp_path, factory=lambda s: DownSource(name=s.name))
    with pytest.raises(SourceUnavailable):
        await svc.source_connect(SourceSpec(name="down", url=URL))
    assert "down" not in svc.sources
    assert all(d["name"] != "down" for d in svc.source_list())


async def test_reserved_name_refused(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError, match="reserved"):
        await svc.source_connect(SourceSpec(name="default", url=URL))
    with pytest.raises(SourceError, match="reserved"):
        await svc.source_disconnect("default")


async def test_duplicate_refused_before_probing(tmp_path):
    probes = []

    class Counting(FakeSource):
        async def probe(self) -> dict:
            probes.append(self.name)
            return {"reachable": True}

    svc = make_service(tmp_path, factory=lambda s: Counting(name=s.name))
    await svc.source_connect(SourceSpec(name="play", url=URL))
    with pytest.raises(SourceError, match="already exists"):
        await svc.source_connect(SourceSpec(name="play", url=URL))
    assert probes == ["play"]
    await svc.source_connect(SourceSpec(name="play", url=URL + "/v2"), replace=True)
    assert probes == ["play", "play"]


async def test_disconnect_removes_and_logs(tmp_path):
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="play", url=URL))
    await svc.source_disconnect("play")
    assert "play" not in svc.sources
    assert any(e.type == "source.disconnected" for e in svc.log.since(0, 100))


async def test_status_reports_unreachable_without_raising(tmp_path):
    svc = make_service(tmp_path)
    svc.sources.attach("down", DownSource(name="down"))
    out = await svc.source_status("down")
    assert out["reachable"] is False
    assert out["hint"] == "check the URL"
    with pytest.raises(SourceError, match="unknown source"):
        await svc.source_status("ghost")


async def test_secret_never_in_list_or_events(tmp_path, monkeypatch):
    monkeypatch.setenv("TN_TEST_TOKEN", "s3cr3t-value")
    svc = make_service(tmp_path)
    await svc.source_connect(SourceSpec(name="auth", url=URL, auth=AuthRef(env="TN_TEST_TOKEN")))
    blob = json.dumps(svc.source_list()) + json.dumps([e.to_dict() for e in svc.log.since(0, 100)])
    assert "s3cr3t-value" not in blob
    assert "TN_TEST_TOKEN" in blob
