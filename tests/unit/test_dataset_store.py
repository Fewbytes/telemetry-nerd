import itertools

import pyarrow as pa
import pytest

from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
from telemetry_nerd.model.time import TimeRange


@pytest.fixture
def store(tmp_path):
    counter = itertools.count(1)
    return DatasetStore(
        open_duckdb(tmp_path / "s.duckdb"), lambda p: f"{p}{next(counter)}", clock=lambda: 7
    )


def result():
    buckets = pa.table(
        {
            "ts_ms": [60_000, 120_000],
            "series_id": ["s1", "s1"],
            "avg": [1.0, None],
            "min": [0.5, None],
            "max": [2.0, None],
            "count": [4, 0],
        },
        schema=BUCKET_SCHEMA,
    )
    series = pa.table({"series_id": ["s1"], "labels": ['{"i":"a"}']}, schema=SERIES_SCHEMA)
    return FetchResult(buckets, series)


def test_put_then_get_roundtrip(store):
    meta = store.put(
        source="vm",
        expr="up",
        rng=TimeRange(60_000, 120_000),
        step_ms=60_000,
        resolution_ms=15_000,
        result=result(),
    )
    assert meta.id == "d1"
    assert meta.representation == "bucket_agg"
    got_meta, got = store.get("d1")
    assert got_meta == meta
    assert got.buckets.equals(result().buckets)
    assert got.series.equals(result().series)


def test_datasets_are_immutable_snapshots(store):
    store.put(
        source="vm",
        expr="up",
        rng=TimeRange(60_000, 120_000),
        step_ms=60_000,
        resolution_ms=15_000,
        result=result(),
    )
    store.put(
        source="vm",
        expr="up",
        rng=TimeRange(60_000, 120_000),
        step_ms=60_000,
        resolution_ms=15_000,
        result=result(),
    )
    assert store.get("d1")[1].buckets.num_rows == 2
    assert store.get("d2")[1].buckets.num_rows == 2


def test_unknown_dataset(store):
    with pytest.raises(NotFound):
        store.get("d404")
