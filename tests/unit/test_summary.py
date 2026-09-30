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
