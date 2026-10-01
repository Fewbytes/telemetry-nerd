import itertools
import math

import pyarrow as pa
import pytest

from telemetry_nerd.analysis.histogram import from_matrix
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


def _dist():
    return from_matrix(
        "s",
        [
            {"metric": {"le": "1"}, "values": [[60, "3"]]},
            {"metric": {"le": "+Inf"}, "values": [[60, "5"], [120, "0"]]},
            {"metric": {"le": "1"}, "values": [[120, "0"]]},
        ],
        expr="e",
    )


def test_distribution_round_trip_keeps_infinite_edges_and_zero_columns(store):
    meta = store.put_distribution(
        source="s", rng=TimeRange(60_000, 120_000), step_ms=60_000, resolution_ms=15_000,
        dist=_dist(), histogram={"selector": "x_bucket", "by": []}, n_min=20,
    )  # fmt: skip
    m2, d2 = store.get_distribution(meta.id)
    assert m2.representation == "distribution" and m2.n_min == 20
    assert m2.scheme["kind"] == "classic" and m2.histogram == {"selector": "x_bucket", "by": []}
    assert [(r["bucket_lo"], r["bucket_hi"], r["count"]) for r in d2.rows.to_pylist()] == [
        (-math.inf, 1.0, 3.0),
        (1.0, math.inf, 2.0),
    ]
    assert [(c["ts_ms"], c["n"]) for c in d2.columns.to_pylist()] == [(60_000, 5.0), (120_000, 0.0)]
    assert d2.scheme.edges == (1.0,)
    assert store.series_count(meta.id) == 1
    with pytest.raises(ValueError, match="distribution"):
        store.get(meta.id)
