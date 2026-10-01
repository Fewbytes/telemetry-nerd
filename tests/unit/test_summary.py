import json

import pyarrow as pa

from telemetry_nerd.core.summary import summarize
from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json

NOW = 10_000_000
STEP = 1000


def meta(start=0, end=4000, step=STEP, resolution=STEP):
    return DatasetMeta(
        id="d1", source="s", expr="up", start_ms=start, end_ms=end,
        step_ms=step, resolution_ms=resolution,
    )  # fmt: skip


def result(rows, names):
    """rows: (ts, series_id, avg, min, max, count)"""
    cols = list(zip(*rows, strict=True)) if rows else [[]] * 6
    buckets = pa.table(dict(zip(BUCKET_SCHEMA.names, cols, strict=True)), schema=BUCKET_SCHEMA)
    series = pa.table(
        {"series_id": list(names), "labels": [labels_json({"n": n}) for n in names.values()]},
        schema=SERIES_SCHEMA,
    )
    return FetchResult(buckets, series)


def run(m, r, **kw):
    return summarize(m, r, now_ms=NOW, settle_ms=300_000, **kw)


def test_empty_result():
    out = run(meta(), result([], {}))
    assert out["series_count"] == 0
    assert out["series"] == []
    assert "empty" in out["caveats"]


def test_gaps_from_missing_buckets_and_zero_counts():
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in (0, 1000, 2000, 3000, 4000)]
    # b: missing 1000 and 3000; count==0 at 2000
    rows += [
        (0, "b", 1.0, 1.0, 1.0, 1),
        (2000, "b", 0.0, 0.0, 0.0, 0),
        (4000, "b", 1.0, 1.0, 1.0, 1),
    ]
    out = run(meta(), result(rows, {"a": "a", "b": "b"}))
    gaps = {s["labels"]["n"]: s["gaps"] for s in out["series"]}
    assert gaps == {"a": 0, "b": 3}
    assert out["caveats"][0] == "gaps"


def test_no_gaps_no_caveat():
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in (0, 1000, 2000, 3000, 4000)]
    assert "gaps" not in run(meta(), result(rows, {"a": "a"}))["caveats"]


def test_count_weighted_mean():
    rows = [(0, "a", 1.0, 1.0, 1.0, 1), (1000, "a", 4.0, 4.0, 4.0, 3)]
    out = run(meta(end=1000), result(rows, {"a": "a"}))
    assert out["series"][0]["mean"] == 3.25  # (1*1 + 4*3) / 4, not (1+4)/2


def test_mean_null_when_all_counts_zero():
    rows = [(0, "a", 0.0, 0.0, 0.0, 0)]
    out = run(meta(end=0), result(rows, {"a": "a"}))
    assert out["series"][0]["mean"] is None
    assert out["series"][0]["gaps"] == 1


def test_top_ordering_and_more_series():
    names = {f"s{k}": f"s{k}" for k in range(7)}
    rows = [(0, f"s{k}", 1.0, 0.0, float(k), 1) for k in range(7)]
    out = run(meta(end=0), result(rows, names), top=3)
    assert out["series_count"] == 7
    assert out["more_series"] == 4
    assert [s["max"] for s in out["series"]] == [6.0, 5.0, 4.0]


def test_fake_resolution_and_settling():
    rows = [(0, "a", 1.0, 1.0, 1.0, 1)]
    m = meta(end=0, step=1000, resolution=60_000)
    m = DatasetMeta(**{**m.to_dict(), "start_ms": NOW - 1000, "end_ms": NOW - 1000})
    out = run(m, result([(NOW - 1000, "a", 1.0, 1.0, 1.0, 1)], {"a": "a"}))
    assert {"fake_resolution", "settling"} <= set(out["caveats"])
    del rows


def test_clean_has_no_caveats():
    rows = [(0, "a", 1.0, 1.0, 1.0, 1)]
    assert run(meta(end=0), result(rows, {"a": "a"}))["caveats"] == []


def test_non_finite_buckets_flag_caveat_and_keep_mean_unbiased():
    nan = float("nan")
    rows = [
        (0, "a", 2.0, 2.0, 2.0, 1),
        (1000, "a", None, None, None, 9),  # adapter nulled a NaN bucket; count kept
        (2000, "a", nan, nan, nan, 9),  # raw NaN must be treated the same
    ]
    out = run(meta(end=2000), result(rows, {"a": "a"}))
    assert "non_finite" in out["caveats"]
    assert out["series"][0]["mean"] == 2.0  # not 2*1/19
    assert out["series"][0]["min"] == 2.0
    assert json.dumps(out, allow_nan=False)


def test_finite_data_has_no_non_finite_caveat():
    rows = [(0, "a", 1.0, 1.0, 1.0, 1)]
    assert "non_finite" not in run(meta(end=0), result(rows, {"a": "a"}))["caveats"]


def test_partial_meta_flags_caveat():
    m = DatasetMeta(**{**meta(end=0).to_dict(), "partial": 2})
    out = run(m, result([(0, "a", 1.0, 1.0, 1.0, 1)], {"a": "a"}))
    assert "partial" in out["caveats"]


def _quantile_inputs(counts, values, n_min=200):
    import pyarrow as pa

    from telemetry_nerd.datasets.store import DatasetMeta
    from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult

    ts = [1_000 + 60_000 * i for i in range(len(values))]
    buckets = pa.table(
        {
            "ts_ms": ts,
            "series_id": ["a"] * len(ts),
            "avg": values,
            "min": values,
            "max": values,
            "count": counts,
        },
        schema=BUCKET_SCHEMA,
    )
    series = pa.table({"series_id": ["a"], "labels": ['{"r":"x"}']}, schema=SERIES_SCHEMA)
    meta = DatasetMeta(
        id="d1",
        source="s",
        expr="histogram_quantile(0.95, x)",
        start_ms=ts[0],
        end_ms=ts[-1],
        step_ms=60_000,
        resolution_ms=15_000,
        representation="quantile",
        quantile=0.95,
        n_min=n_min,
    )
    return meta, FetchResult(buckets, series)


def test_quantile_summary_reports_n_and_never_averages():
    meta, result = _quantile_inputs([10, 300, 500, 12], [34.0, 0.6, 0.7, 0.2])
    s = summarize(meta, result, now_ms=10**13, settle_ms=0)
    [row] = s["series"]
    assert s["quantile"] == 0.95 and s["n_min"] == 200
    assert row["mean"] is None
    assert row["n_total"] == 822
    assert (row["meaningful_buckets"], row["buckets"]) == (2, 4)
    assert (row["min"], row["max"]) == (0.6, 0.7)  # the n=10 spike is not a meaningful p95
    assert "low_count" in s["caveats"]


def test_quantile_summary_without_n():
    meta, result = _quantile_inputs([1, 1], [0.3, 0.4], n_min=None)
    s = summarize(meta, result, now_ms=10**13, settle_ms=0)
    assert "n_unknown" in s["caveats"]
    assert (s["series"][0]["min"], s["series"][0]["max"]) == (0.3, 0.4)
