import pyarrow as pa
import pytest
from pydantic import ValidationError

from telemetry_nerd.analysis.reference import WEEK_MS
from telemetry_nerd.charts.indexed import check_index, shifted, window_baselines
from telemetry_nerd.charts.yview import YView
from telemetry_nerd.model.series import BUCKET_SCHEMA


def tbl(rows):  # (ts, series, avg, count)
    return pa.table(
        {
            "ts_ms": [r[0] for r in rows],
            "series_id": [r[1] for r in rows],
            "avg": [r[2] for r in rows],
            "min": [r[2] for r in rows],
            "max": [r[2] for r in rows],
            "count": [r[3] for r in rows],
        },
        schema=BUCKET_SCHEMA,
    )


def test_indexed_view_needs_a_baseline_and_only_indexed_takes_one():
    YView(mode="indexed", label="÷ own mean", baseline="window")
    with pytest.raises(ValidationError, match="baseline"):
        YView(mode="indexed", label="i")
    with pytest.raises(ValidationError, match="baseline"):
        YView(mode="log", label="l", baseline="week")


def test_window_baseline_is_the_count_weighted_mean():
    assert window_baselines(tbl([(1, "a", 1.0, 1), (2, "a", 4.0, 3)])) == {"a": 3.25}


def test_window_refused_for_percentiles():
    with pytest.raises(ValueError, match="average percentiles"):
        check_index("window", "quantile", 200, tbl([(1, "a", 1.0, 300)]), None, {})


def test_non_positive_or_missing_baseline_is_named_and_all_bad_is_refused():
    t = tbl([(1, "a", 0.0, 4), (1, "b", 2.0, 4)])
    assert check_index("window", "bucket_agg", None, t, None, {"a": 'instance="a"'}) == [
        'not indexed (baseline missing or ≤ 0): instance="a"'
    ]
    with pytest.raises(ValueError, match="nothing to index"):
        check_index("window", "bucket_agg", None, tbl([(1, "a", -1.0, 4)]), None, {})


def test_pointwise_week_baseline_shifts_onto_the_grid_and_gates_n():
    cur = tbl([(WEEK_MS + 1, "a", 2.0, 300), (WEEK_MS + 2, "a", 2.0, 300)])
    ref = shifted(tbl([(1, "a", 1.0, 300), (2, "a", 1.0, 50)]), WEEK_MS)
    assert ref["ts_ms"].to_pylist() == [WEEK_MS + 1, WEEK_MS + 2]
    assert check_index("week", "quantile", 200, cur, ref, {}) == [
        "1 step(s) without a usable baseline are not drawn"
    ]
    with pytest.raises(ValueError, match="no .*week.* reference"):
        check_index("week", "bucket_agg", None, cur, None, {})
