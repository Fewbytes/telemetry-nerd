import itertools
import math

import polars as pl
from hypothesis import given
from hypothesis import strategies as st

from telemetry_nerd.analysis.distlod import merge_values, rebucket_time
from telemetry_nerd.model.distribution import BucketScheme

INF = math.inf


def _rows(buckets, counts, sid="a", ts=1):
    return pl.DataFrame(
        {
            "series_id": [sid] * len(buckets),
            "ts_ms": [ts] * len(buckets),
            "bucket_lo": [float(b[0]) for b in buckets],
            "bucket_hi": [float(b[1]) for b in buckets],
            "count": [float(c) for c in counts],
        }
    )


def test_time_rebucket_sums_counts_n_and_cover():
    rows = pl.DataFrame(
        {"series_id": ["a"] * 3, "ts_ms": [60_000, 120_000, 180_000],
         "bucket_lo": [0.0] * 3, "bucket_hi": [1.0] * 3, "count": [1.0, 2.0, 4.0]}
    )  # fmt: skip
    cols = pl.DataFrame(
        {"series_id": ["a"] * 3, "ts_ms": [60_000, 120_000, 180_000],
         "n": [1.0, 2.0, 4.0], "cover": [1, 1, 1]}
    )  # fmt: skip
    r, c = rebucket_time(rows, cols, 120_000)
    assert r.select("ts_ms", "count").rows() == [(120_000, 3.0), (240_000, 4.0)]
    assert c.select("ts_ms", "n", "cover").rows() == [(120_000, 3.0, 2), (240_000, 4.0, 1)]


def test_classic_merge_keeps_every_mth_edge_and_open_buckets():
    edges = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
    buckets = [(-INF, 0.1), (0.1, 0.2), (0.2, 0.5), (0.5, 1.0), (5.0, 10.0), (10.0, INF)]
    out, m = merge_values(
        _rows(buckets, [1, 2, 3, 4, 5, 6]), BucketScheme("classic", edges=edges), 3
    )
    assert m == 2
    assert out.select("bucket_lo", "bucket_hi", "count").rows() == [
        (-INF, 0.1, 1.0), (0.1, 0.5, 5.0), (0.5, 2.0, 4.0), (2.0, 10.0, 5.0), (10.0, INF, 6.0)
    ]  # fmt: skip


def test_no_merge_when_rows_fit():
    rows = _rows([(0.1, 0.2)], [1])
    out, m = merge_values(rows, BucketScheme("classic", edges=(0.1, 0.2)), 10)
    assert m == 1 and out.equals(rows)


@given(st.lists(st.integers(0, 50), min_size=2, max_size=12), st.integers(1, 6))
def test_merge_preserves_total_and_respects_the_row_budget(counts, max_rows):
    edges = tuple(float(2**i) for i in range(len(counts) + 1))
    rows = _rows(list(itertools.pairwise(edges)), counts)
    out, _ = merge_values(rows, BucketScheme("classic", edges=edges), max_rows)
    assert out["count"].sum() == sum(counts)
    assert out.height <= max_rows
