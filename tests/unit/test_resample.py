# tests/unit/test_resample.py
import math

import pyarrow as pa
from hypothesis import given, settings
from hypothesis import strategies as st

from telemetry_nerd.analysis.resample import lod, rebucket
from telemetry_nerd.model.series import BUCKET_SCHEMA
from telemetry_nerd.model.time import TimeRange

STEP = 15_000


def table(rows):
    cols = {name: [r[i] for r in rows] for i, name in enumerate(BUCKET_SCHEMA.names)}
    return pa.table(cols, schema=BUCKET_SCHEMA)


def test_rebucket_aggregates_correctly():
    t = table(
        [
            (15_000, "s", 1.0, 0.0, 2.0, 1),
            (30_000, "s", 4.0, 3.0, 10.0, 3),
            (45_000, "s", None, None, None, 0),
            (60_000, "s", 2.0, 1.0, 3.0, 2),
        ]
    )
    out = rebucket(t, 60_000).to_pylist()
    assert out == [
        {
            "ts_ms": 60_000,
            "series_id": "s",
            "avg": (1 + 12 + 4) / 6,
            "min": 0.0,
            "max": 10.0,
            "count": 6,
        }
    ]


def test_rebucket_all_empty_bucket_has_null_avg():
    t = table([(15_000, "s", None, None, None, 0)])
    assert rebucket(t, 60_000).to_pylist()[0]["avg"] is None


def test_rebucket_keeps_series_separate():
    t = table([(60_000, "a", 1.0, 1.0, 1.0, 1), (60_000, "b", 5.0, 5.0, 5.0, 1)])
    out = rebucket(t, 60_000).to_pylist()
    assert [r["series_id"] for r in out] == ["a", "b"]


def test_lod_noop_when_it_fits():
    t = table([(i * STEP, "s", 1.0, 1.0, 1.0, 1) for i in range(1, 11)])
    out, step = lod(t, STEP, TimeRange(STEP, 10 * STEP), width_px=800)
    assert step == STEP
    assert out.equals(t)


def test_lod_reduces_to_width_and_keeps_peak():
    rows = [(i * STEP, "s", 1.0, 1.0, 1.0, 4) for i in range(1, 1001)]
    rows[500] = (501 * STEP, "s", 1.0, 1.0, 999.0, 4)
    out, step = lod(table(rows), STEP, TimeRange(STEP, 1000 * STEP), width_px=100)
    assert step == STEP * 10
    assert out.num_rows <= 101
    assert max(out.column("max").to_pylist()) == 999.0


bucket = st.tuples(
    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
    st.floats(0, 1e3, allow_nan=False, allow_infinity=False),
    st.integers(0, 50),
)


@settings(max_examples=200)
@given(st.dictionaries(st.integers(1, 200), bucket, min_size=1, max_size=200), st.integers(1, 20))
def test_rebucket_invariants(data, factor):
    rows = []
    for i, (avg, spread, count) in sorted(data.items()):
        if count == 0:
            rows.append((i * STEP, "s", None, None, None, 0))
        else:
            rows.append((i * STEP, "s", avg, avg - spread, avg + spread, count))
    t = table(rows)
    out = rebucket(t, STEP * factor)

    def col(tbl, name):
        return [v for v in tbl.column(name).to_pylist() if v is not None]

    assert sum(col(out, "count")) == sum(col(t, "count"))
    if col(t, "min"):
        assert min(col(out, "min")) == min(col(t, "min"))
        assert max(col(out, "max")) == max(col(t, "max"))
    total = sum(col(t, "count"))
    if total:
        before = sum(r[2] * r[5] for r in rows if r[5]) / total
        after_rows = [r for r in out.to_pylist() if r["count"]]
        after = sum(r["avg"] * r["count"] for r in after_rows) / total
        assert math.isclose(before, after, rel_tol=1e-9, abs_tol=1e-6)


def test_rebucket_mixes_count_only_and_valued_buckets_without_bias():
    """1h9.16: a count-only (null value) bucket carries no mean; NaN never poisons a merge."""
    import math

    import pyarrow as pa

    from telemetry_nerd.analysis.resample import rebucket
    from telemetry_nerd.model.series import BUCKET_SCHEMA

    def tbl(rows):
        cols = list(zip(*rows, strict=True))
        return pa.table(
            dict(
                zip(
                    ["ts_ms", "series_id", "avg", "min", "max", "count"],
                    map(list, cols),
                    strict=True,
                )
            ),
            schema=BUCKET_SCHEMA,
        )

    nan = math.nan
    out = rebucket(
        tbl(
            [
                (1000, "a", 2.0, 1.0, 3.0, 2),
                (2000, "a", None, None, None, 5),
                (3000, "b", None, None, None, 5),
                (4000, "b", None, None, None, 5),
                (5000, "c", nan, nan, nan, 5),
                (6000, "c", 4.0, 4.0, 4.0, 1),
                (7000, "d", nan, nan, nan, 5),
                (8000, "d", nan, nan, nan, 5),
            ]
        ),
        10_000,
    ).to_pylist()
    by = {r["series_id"]: r for r in out}
    assert (by["a"]["avg"], by["a"]["min"], by["a"]["max"], by["a"]["count"]) == (2.0, 1.0, 3.0, 7)
    assert (by["b"]["avg"], by["b"]["count"]) == (None, 10)
    assert (by["c"]["avg"], by["c"]["max"]) == (4.0, 4.0)
    assert math.isnan(by["d"]["avg"]) and by["d"]["count"] == 10
