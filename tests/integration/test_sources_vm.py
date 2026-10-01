import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration


async def test_connect_second_source_at_runtime_and_survive_restart(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    samples = [(t0 + i * 15_000, 1.0) for i in range(240)]
    push(vm_url, exposition("tn_it_rt", {"instance": "a"}, samples))
    # the settings-owned default points nowhere: only the runtime source can answer
    settings = Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9")
    svc = build_service(settings)

    out = await svc.source_connect(SourceSpec(name="vm", url=vm_url, flavor="victoriametrics"))
    assert out["status"]["reachable"] is True

    res = await svc.query('tn_it_rt{instance="a"}', "now-110m", "now-60m", step="1m", source="vm")
    assert res["summary"]["series_count"] == 1

    restarted = build_service(settings)
    assert "vm" in restarted.sources
    assert (await restarted.source_status("vm"))["reachable"] is True
    assert (await restarted.source_status("default"))["reachable"] is False
