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


def test_failed_spans_round_trip(store):
    base = result()
    failed = ((1000, 2000, "SourceUnavailable: x"),)
    meta = store.put(
        source="s",
        expr="up",
        rng=TimeRange(0, 60_000),
        step_ms=1000,
        resolution_ms=1000,
        result=FetchResult(base.buckets, base.series, failed=failed),
    )
    assert store.meta(meta.id).failed_spans == [[1000, 2000, "SourceUnavailable: x"]]


def test_old_meta_without_failed_spans_loads(store):
    meta = store.put(
        source="s",
        expr="up",
        rng=TimeRange(0, 60_000),
        step_ms=1000,
        resolution_ms=1000,
        result=result(),
    )
    assert store.meta(meta.id).failed_spans == []


import json


def test_query_language_and_extra_caveats_are_recorded(store):
    meta = store.put(
        source="es", expr='{"query": {"match_all": {}}}', rng=TimeRange(60_000, 120_000),
        step_ms=60_000, resolution_ms=1_000, result=result(),
        caveats=["zero_is_no_documents"], query_language="es_dsl",
    )  # fmt: skip
    got = store.meta(meta.id)
    assert got.query_language == "es_dsl"
    assert got.source_caveats == ["zero_is_no_documents"]


def test_datasets_stored_before_query_language_existed_read_as_promql(store):
    meta = store.put(
        source="vm", expr="up", rng=TimeRange(60_000, 120_000), step_ms=60_000,
        resolution_ms=15_000, result=result(),
    )  # fmt: skip
    old = {k: v for k, v in meta.to_dict().items() if k != "query_language"}
    store._con.execute("UPDATE datasets SET meta = $m WHERE id = $id",
                       {"m": json.dumps(old), "id": meta.id})  # fmt: skip
    assert store.meta(meta.id).query_language == "promql"


def test_distribution_records_its_query_language(store):
    from telemetry_nerd.model.distribution import (
        COLUMN_SCHEMA,
        DIST_SCHEMA,
        BucketScheme,
        DistResult,
    )

    dist = DistResult(DIST_SCHEMA.empty_table(), COLUMN_SCHEMA.empty_table(),
                      SERIES_SCHEMA.empty_table(), BucketScheme("linear", width=5.0), "{}")  # fmt: skip
    meta = store.put_distribution(
        source="es", rng=TimeRange(60_000, 120_000), step_ms=60_000, resolution_ms=1_000,
        dist=dist, histogram=None, n_min=20, query_language="es_dsl",
    )  # fmt: skip
    assert store.meta(meta.id).query_language == "es_dsl"
