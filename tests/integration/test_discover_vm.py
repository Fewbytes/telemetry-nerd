import pytest

from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.promql import PromQLSource

pytestmark = pytest.mark.integration


async def test_discover_and_scrape_interval_against_vm(vm_url):
    t0 = (now_ms() - 30 * 60_000) // 15_000 * 15_000
    push(
        vm_url,
        exposition(
            "tn_it_discover", {"instance": "a"}, [(t0 + i * 15_000, 1.0) for i in range(100)]
        ),
    )
    src = PromQLSource("vm", vm_url)
    try:
        d = await src.discover()
        assert "tn_it_discover" in {m.name for m in d.metrics}
        assert "instance" in d.label_names
        assert d.origin == "source-metadata"
        assert await src.scrape_interval("tn_it_discover") == 15_000
    finally:
        await src.aclose()
