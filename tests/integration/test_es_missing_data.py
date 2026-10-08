"""Evidence for `sources/semantics.py`'s `ELASTICSEARCH` profile (missing-data semantics, bead
telemetry-nerd-sgb.6): retention edge and settling, reproduced against real Elasticsearch 8.x and
OpenSearch 2.x clusters. Partial-shard/timeout detection is unit-tested (tests/unit/test_es_fetch.py)
against the documented `_shards`/`timed_out` response shape; a real cluster cannot be made to fail
a shard or time out on demand without destructive fault injection (see `ELASTICSEARCH.notes`)."""

import json
import time

import httpx
import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.spec import SourceSpec
from tests.integration.es_seed import PATTERN, base_ms, seed

pytestmark = pytest.mark.integration


@pytest.fixture(params=["elasticsearch", "opensearch"])
def cluster(request):
    return request.param, request.getfixturevalue(
        "es_url" if request.param == "elasticsearch" else "os_url"
    )


async def test_retention_edge_is_empty_not_error(cluster, tmp_path):
    """A query range entirely before the earliest document answers an empty success (never an
    error), as every PromQL-family backend does: the same `bucket_state` "cannot tell no-traffic
    from no-ingestion" treatment applies."""
    flavor, url = cluster
    base = base_ms()
    seed(url, base)
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(SourceSpec(name="es", url=url, flavor=flavor,
                                        index_pattern=PATTERN, time_field="@timestamp"))  # fmt: skip
    before = base - 10 * 24 * 3_600_000
    out = await svc.query(json.dumps({"query": {"match_all": {}}}),
                          start=str(before), end=str(before + 5 * 60_000), step="1m", source="es")  # fmt: skip
    _, res = svc.datasets.get(out["dataset"])
    assert res.buckets.num_rows == 0 and res.series.num_rows == 0


def test_document_is_invisible_until_the_next_refresh(cluster):
    """`index.refresh_interval` (default 1s) governs visibility: a document already stored and
    acknowledged is not searchable (so not aggregatable) until the index's next refresh. The
    adapter does not read this setting or expose an ingest-lag number (spec: later-work #6):
    this is the real mechanism behind "late ingestion / settling" for this backend."""
    _flavor, url = cluster
    index = f"tn-settling-{int(time.time() * 1000)}"
    httpx.delete(f"{url}/{index}", timeout=30)
    httpx.put(
        f"{url}/{index}",
        json={"mappings": {"properties": {"@timestamp": {"type": "date"}}}},
        timeout=30,
    ).raise_for_status()
    try:
        httpx.post(
            f"{url}/{index}/_doc?refresh=false",
            json={"@timestamp": int(time.time() * 1000)},
            timeout=30,
        ).raise_for_status()
        before = httpx.post(f"{url}/{index}/_search",
                            json={"size": 0, "query": {"match_all": {}}}, timeout=30)  # fmt: skip
        assert before.json()["hits"]["total"]["value"] == 0
        httpx.post(f"{url}/{index}/_refresh", timeout=30).raise_for_status()
        after = httpx.post(f"{url}/{index}/_search",
                           json={"size": 0, "query": {"match_all": {}}}, timeout=30)  # fmt: skip
        assert after.json()["hits"]["total"]["value"] == 1
    finally:
        httpx.delete(f"{url}/{index}", timeout=30)
