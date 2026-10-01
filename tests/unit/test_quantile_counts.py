import pyarrow as pa

from telemetry_nerd.analysis.quantile import attach_counts
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult


def result(rows):
    buckets = pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "avg": [r[2] for r in rows],
            "min": [r[2] for r in rows],
            "max": [r[2] for r in rows],
            "count": [1] * len(rows),
        },
        schema=BUCKET_SCHEMA,
    )
    sids = sorted({r[1] for r in rows})
    series = pa.table({"series_id": sids, "labels": ["{}"] * len(sids)}, schema=SERIES_SCHEMA)
    return FetchResult(buckets, series)


def test_counts_replace_placeholder_and_missing_is_zero():
    values = result([(1, "a", 0.3), (2, "a", 0.4), (1, "b", 0.9)])
    counts = result([(1, "a", 250.4), (2, "a", None), (9, "a", 7.0)])
    out = attach_counts(values, counts).buckets.to_pylist()
    got = {(r["series_id"], r["ts_ms"]): (r["avg"], r["count"]) for r in out}
    assert got == {("a", 1): (0.3, 250), ("a", 2): (0.4, 0), ("b", 1): (0.9, 0)}


def test_series_table_comes_from_values():
    values = result([(1, "a", 0.3)])
    out = attach_counts(values, result([(1, "zzz", 5.0)]))
    assert out.series.to_pylist() == values.series.to_pylist()
