import json

import pytest

from telemetry_nerd.sources.base import SourceError
from tests.unit.fakes import FakeSource, make_service


async def test_query_distribution_builds_dataset_and_summary(tmp_path):
    src = FakeSource()
    svc = make_service(tmp_path, src)
    out = await svc.query_distribution(
        'lat_seconds_bucket{job="a"}', by=["instance"], start="now-2h", end="now-1h", step="1m"
    )
    meta, _ = svc.datasets.get_distribution(out["dataset"])
    assert meta.expr == 'sum by (le, vmrange, instance) (increase(lat_seconds_bucket{job="a"}[1m]))'
    assert meta.histogram == {"selector": 'lat_seconds_bucket{job="a"}', "by": ["instance"]}
    s = out["summary"]
    assert s["representation"] == "distribution" and s["n_min"] == 20
    assert s["buckets"] == "classic le buckets: 0.1, 1, 10"
    row = s["series"][0]  # sorted by n_total: instance i1 (x2)
    assert row["labels"] == {"instance": "i1"}
    assert (row["columns"], row["missing_columns"], row["low_n_columns"]) == (61, 0, 0)
    assert row["n_total"] == 200 * 61
    assert row["quantile_buckets"] == {
        "p50": ["-Inf", 0.1],
        "p90": ["-Inf", 0.1],
        "p99": [0.1, 1.0],
    }
    assert len(json.dumps(out)) < 2048
    assert src.hist_selectors == ['lat_seconds_bucket{job="a"}']


async def test_distribution_step_must_cover_two_scrapes(tmp_path):
    svc = make_service(tmp_path, FakeSource(resolution_ms=30_000))
    with pytest.raises(SourceError, match="two scrape"):
        await svc.query_distribution("x_bucket", start="now-2h", end="now-1h", step="30s")
    out = await svc.query_distribution("x_bucket", start="now-2h", end="now-1h")
    assert svc.datasets.meta(out["dataset"]).step_ms >= 60_000


async def test_unknown_source_is_explained(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(SourceError, match="unknown source"):
        await svc.query_distribution("x_bucket", source="nope")
