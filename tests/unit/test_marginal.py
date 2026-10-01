import math

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.marginal import histogram_of, pooled_window, sample_bins, step_values
from telemetry_nerd.model.series import BUCKET_SCHEMA


def tbl(avg, count, sids=None):
    n = len(avg)
    return pa.table(
        {
            "ts_ms": list(range(n)),
            "series_id": sids or ["a"] * n,
            "avg": avg,
            "min": avg,
            "max": avg,
            "count": count,
        },
        schema=BUCKET_SCHEMA,
    )


def test_step_values_pool_series_and_drop_low_n_percentiles():
    vals, excluded, k = step_values(
        tbl([0.4, None, 34.0, 0.9], [300, 5, 13, 400], ["a", "a", "b", "b"]), "quantile", 200
    )
    assert (sorted(vals), excluded, k) == (
        [0.4, 0.9],
        1,
        2,
    )  # null is not a value; n=13 is excluded
    vals, excluded, _ = step_values(tbl([1.0, float("nan"), 2.0], [4, 4, 4]), "bucket_agg", None)
    assert (vals, excluded) == ([1.0, 2.0], 0)


def test_sample_bins_share_edges_and_go_log_past_two_decades():
    lin = sample_bins([1.0, 2.0], [3.0], bins=4)
    assert lin[0][0] == 1.0 and lin[-1][1] == 3.0 and len(lin) == 4
    log = sample_bins([0.01, 1.0], [5.0], bins=10)
    widths = [h / lo for lo, h in log]
    assert all(math.isclose(w, widths[0]) for w in widths)  # equal ratio = log-spaced
    assert sample_bins([2.0, 2.0], [], bins=8) == [(1.98, 2.02)]
    assert sample_bins([], []) == []


def test_histogram_counts_every_value_once_edges_inclusive():
    edges = sample_bins([1.0, 2.0, 3.0], [], bins=2)
    assert histogram_of([1.0, 2.0, 3.0], edges) == [2, 1]  # (lo, hi]; first bin also takes lo


def test_pooled_window_sums_series_counts():
    rows = pl.DataFrame(
        {
            "series_id": ["a", "b"],
            "ts_ms": [60_000, 60_000],
            "bucket_lo": [0.0, 0.0],
            "bucket_hi": [0.1, 0.1],
            "count": [3.0, 5.0],
        }
    )
    cols = pl.DataFrame({"series_id": ["a", "b"], "ts_ms": [60_000, 60_000], "n": [3.0, 5.0]})
    w = pooled_window(rows, cols, 60_000, 0, 60_000)
    assert (w["n"], w["c"], w["series"]) == (8.0, [8.0], 2)
