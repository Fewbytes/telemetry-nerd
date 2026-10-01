import math

import polars as pl

from telemetry_nerd.analysis.distlod import rebucket_time
from telemetry_nerd.analysis.quantiles import column_quantiles, q_key, quantile_bucket

INF = math.inf


def frames(cells, cols):
    """cells: (sid, ts, lo, hi, count); cols: (sid, ts, n)."""
    rows = pl.DataFrame(
        cells, schema=["series_id", "ts_ms", "bucket_lo", "bucket_hi", "count"], orient="row"
    )
    return rows, pl.DataFrame(cols, schema=["series_id", "ts_ms", "n"], orient="row")


def test_quantile_bucket_is_the_containing_source_bucket():
    b = [(-INF, 0.1, 6.0), (0.1, 1.0, 3.0), (1.0, INF, 1.0)]
    assert quantile_bucket(b, 0.5) == (-INF, 0.1)
    assert quantile_bucket(b, 0.6) == (-INF, 0.1)  # exactly on an edge: the lower bucket
    assert quantile_bucket(b, 0.95) == (1.0, INF)
    assert quantile_bucket([], 0.5) is None


def test_bucket_per_column_only_where_n_is_enough():
    rows, cols = frames(
        [
            ("a", 60_000, 1.0, 10.0, 280.0),
            ("a", 60_000, 10.0, INF, 20.0),
            ("a", 120_000, 1.0, 10.0, 100.0),
            ("a", 120_000, 10.0, INF, 50.0),
        ],
        [("a", 60_000, 300.0), ("a", 120_000, 150.0)],
    )
    out = column_quantiles(rows, cols, [0.5, 0.95])
    assert out["a"][q_key(0.95)] == {"ts": [60_000], "lo": [10.0], "hi": [INF]}  # n=150 < 200
    assert out["a"][q_key(0.5)] == {"ts": [60_000, 120_000], "lo": [1.0, 1.0], "hi": [10.0, 10.0]}


def test_gate_uses_column_n_not_bucket_sum():
    # native histograms: n is histogram_count, may differ slightly from the bucket sum
    rows, cols = frames([("a", 60_000, 1.0, 2.0, 20.0)], [("a", 60_000, 19.9)])
    assert column_quantiles(rows, cols, [0.5]) == {}


def test_time_summed_columns_match_the_summed_histogram():
    cells = [
        ("a", t, lo, hi, c)
        for t in (60_000, 120_000)
        for lo, hi, c in ((0.0, 1.0, 600.0), (1.0, 2.0, 390.0), (2.0, 4.0, 10.0))
    ]
    rows, cols = frames(cells, [("a", t, 1000.0) for t in (60_000, 120_000)])
    r2, c2 = rebucket_time(rows, cols.with_columns(pl.lit(1).alias("cover")), 120_000)
    out = column_quantiles(r2, c2, [0.99])
    assert out["a"]["0.99"] == {"ts": [120_000], "lo": [1.0], "hi": [2.0]}  # 1980/2000
