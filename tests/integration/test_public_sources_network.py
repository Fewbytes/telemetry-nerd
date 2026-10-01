"""Connect every public registry source by name over the real network (slow, polite).

Run: just test-integration -k public  (one probe per source, spaced by the registry's gate)
"""

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.public import PUBLIC_SOURCES

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", sorted(PUBLIC_SOURCES))
async def test_connect_by_name_reaches_the_public_source(name, tmp_path):
    svc = build_service(Settings(data_dir=tmp_path, source_url="http://127.0.0.1:9"))
    try:
        out = await svc.source_connect(PUBLIC_SOURCES[name].to_spec())
    finally:
        await svc.source_disconnect(name) if name in svc.sources else None
    assert out["status"]["reachable"] is True
