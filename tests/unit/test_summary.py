import json
from dataclasses import replace

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
    # b: missing 1000 and 3000; count==0 and no value at 2000
    rows += [
        (0, "b", 1.0, 1.0, 1.0, 1),
        (2000, "b", None, None, None, 0),
        (4000, "b", 1.0, 1.0, 1.0, 1),
    ]
    # c: count==0 with a value at 2000: a spilled scrape's bucket kept with the expression's own
    # value (companions.settle_unobserved, uup) is data, not a gap
    rows += [(t, "c", 1.0, 1.0, 1.0, 0 if t == 2000 else 1) for t in (0, 1000, 2000, 3000, 4000)]
    out = run(meta(), result(rows, {"a": "a", "b": "b", "c": "c"}))
    gaps = {s["labels"]["n"]: s["gaps"] for s in out["series"]}
    assert gaps == {"a": 0, "b": 3, "c": 0}
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
    assert out["series"][0]["mean"] is None  # count-weighted: a bucket without samples weighs 0
    assert out["series"][0]["gaps"] == 0  # its value is kept data (uup), not a gap


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
    # the count column is a placeholder: it must not be reported as n
    assert s["series"][0]["n_total"] is None and s["series"][0]["meaningful_buckets"] is None


def test_quantile_value_with_missing_count_is_low_count_not_a_gap():
    meta, result = _quantile_inputs([0, 300], [0.3, 0.4])
    s = summarize(meta, result, now_ms=10**13, settle_ms=0)
    row = s["series"][0]
    assert (row["buckets"], row["meaningful_buckets"], row["gaps"]) == (2, 1, 0)
    assert "low_count" in s["caveats"] and "gaps" not in s["caveats"]


def _dist(cum_by_ts, *, start=60_000, end=180_000, step=60_000):
    from telemetry_nerd.analysis.histogram import from_matrix

    by_le: dict[str, list] = {}
    for t, cum in cum_by_ts.items():
        for le, v in cum.items():
            by_le.setdefault(le, []).append([t / 1000, str(v)])
    dist = from_matrix("s", [{"metric": {"le": le}, "values": v} for le, v in by_le.items()], "e")
    m = DatasetMeta(
        id="d1", source="s", expr="e", start_ms=start, end_ms=end, step_ms=step,
        resolution_ms=15_000, representation="distribution", n_min=20,
        scheme=dist.scheme.to_dict(), source_caveats=list(dist.caveats),
    )  # fmt: skip
    return m, dist


def test_distribution_summary_counts_columns_and_bounds_quantiles():
    from telemetry_nerd.core.summary import summarize_distribution

    m, dist = _dist({60_000: {"1": 15, "+Inf": 30}, 120_000: {"1": 0, "+Inf": 0}})
    s = summarize_distribution(m, dist, now_ms=NOW, settle_ms=0)
    [row] = s["series"]
    assert s["buckets"] == "classic le buckets: 1"
    assert (row["n_total"], row["columns"], row["zero_columns"], row["missing_columns"]) == (
        30,
        2,
        1,
        1,
    )
    assert row["quantile_buckets"] == {"p50": ["-Inf", 1.0]}  # p90 needs n >= 100
    assert s["caveats"][0] == "gaps"
    assert "overflow" in s["caveats"]


def test_distribution_summary_flags_low_n_columns():
    from telemetry_nerd.core.summary import summarize_distribution

    m, dist = _dist({60_000: {"1": 3, "+Inf": 5}}, end=60_000)
    s = summarize_distribution(m, dist, now_ms=NOW, settle_ms=0)
    assert s["series"][0]["low_n_columns"] == 1
    assert s["series"][0]["quantile_buckets"] == {}
    assert "low_count" in s["caveats"]


async def test_summary_reports_coverage(tmp_path):
    from tests.unit.fakes import make_service
    from tests.unit.test_service import HoleySource

    svc = make_service(tmp_path, HoleySource())
    out = await svc.query("up", start="now-2h", end="now-1h", step="1m")
    s = out["summary"]
    assert "missing_data" in s["caveats"]
    cov = {tuple(x["labels"].items()): x["coverage"] for x in s["series"]}
    worst = min(cov.values(), key=lambda c: c["pct"])
    assert worst["longest_gap"] == "3m" and worst["pct"] < 1.0
    assert s["unknown_spans"] == []


def test_jittery_counts_do_not_raise_missing_data():
    counts = [4, 3, 4, 4, 4, 3, 4, 4, 4]
    rows = [(i * STEP, "a", 1.0, 1.0, 1.0, c) for i, c in enumerate(counts)]
    m = meta(end=8000, resolution=250)  # expected 4 per 1s bucket
    out = run(m, result(rows, {"a": "a"}))
    assert "missing_data" not in out["caveats"] and "untrusted_data" not in out["caveats"]
    assert out["series"][0]["coverage"]["missing"] == "0s"


def test_a_hole_is_missing_data_and_unknown_is_untrusted():
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in (0, 1000, 3000, 4000)]
    out = run(meta(), result(rows, {"a": "a"}))
    assert "missing_data" in out["caveats"] and "untrusted_data" not in out["caveats"]
    m = replace(meta(), failed_spans=[[1000, 2000, "boom"]])
    out = run(m, result(rows, {"a": "a"}))
    assert "untrusted_data" in out["caveats"] and out["unknown_spans"] == [
        ["1970-01-01T00:00:00+00:00", "1970-01-01T00:00:02+00:00", "boom"]
    ]


def test_failed_fetch_with_no_rows_is_untrusted_not_just_empty():
    m = replace(meta(), failed_spans=[[1000, 2000, "boom"]])
    out = run(m, result([], {}))
    assert out["caveats"][0] == "empty" and "untrusted_data" in out["caveats"]
    assert len(out["unknown_spans"]) == 1


def test_unknown_spans_are_capped():
    spans = [[t, t, "x"] for t in range(1000, 60_000, 2000)]  # 30 separate spans
    m = replace(meta(end=100_000), failed_spans=spans)
    out = run(m, result([], {}))
    assert len(out["unknown_spans"]) == 10 and out["unknown_spans_more"] == 20


def test_coarse_scrape_and_rate_change_codes_in_summary():
    # configured 250ms, scraped once per 1s bucket: the series' own rate differs
    rows = [(i * STEP, "a", 1.0, 1.0, 1.0, 1) for i in range(5)]
    out = run(meta(resolution=250), result(rows, {"a": "a"}))
    assert "interval_differs" in out["caveats"] and "interval_change" not in out["caveats"]
    # at the configured rate: neither
    rows = [(i * STEP, "a", 1.0, 1.0, 1.0, 4) for i in range(5)]
    out = run(meta(resolution=250), result(rows, {"a": "a"}))
    assert not {"interval_differs", "interval_change"} & set(out["caveats"])
    # rate change within the window
    counts = [4, 4, 4, 4, 1, 1, 1, 1]
    rows = [(i * STEP, "a", 1.0, 1.0, 1.0, c) for i, c in enumerate(counts)]
    out = run(meta(end=7000, resolution=250), result(rows, {"a": "a"}))
    assert "interval_change" in out["caveats"]


def test_series_slower_than_the_step_reads_healthy_in_summary():
    # one sample every 4th 1s bucket (4s scrape), step 1s, configured 1s: not missing
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in range(0, 61_000, 4000)]
    out = run(meta(end=60_000, resolution=1000), result(rows, {"a": "a"}))
    cov = out["series"][0]["coverage"]
    assert cov["pct"] == 1.0 and cov["missing"] == "0s"
    assert "missing_data" not in out["caveats"] and "interval_differs" in out["caveats"]


def test_unknown_spans_name_their_own_reasons():
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in range(0, 10_000, 1000)]
    failed = [[1000, 2000, "timeout"], [5000, 5000, "boom"], [20_000, 30_000, "outside"]]
    out = run(replace(meta(end=9000), failed_spans=failed), result(rows, {"a": "a"}))
    assert [(a[17:19], b[17:19], why) for a, b, why in out["unknown_spans"]] == [
        ("00", "02", "timeout"),
        ("04", "05", "boom"),
    ]


def test_unobservable_expression_unknown_spans_say_why():
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in range(0, 4000, 1000)]
    out = run(replace(meta(end=3000), expr="a / on(job) b"), result(rows, {"a": "a"}))
    assert [why for *_ab, why in out["unknown_spans"]] == ["subquery_fills_gaps"]


def test_silent_members_are_named_up_to_top_and_the_rest_counted():
    # a lost 1 bucket, b 2, c 3, d none; ranked by value (a first), the silent ones are anywhere
    lost = {"a": [4000], "b": [4000, 5000], "c": [4000, 5000, 6000], "d": []}
    rows = [
        (t, sid, 10.0 - k, 10.0 - k, 10.0 - k, 1)
        for k, sid in enumerate("abcd")
        for t in range(0, 10_000, 1000)
        if t not in lost[sid]
    ]
    out = run(meta(end=9000), result(rows, {s: s for s in "abcd"}), top=2)
    assert [(m["labels"], m["silent_for"]) for m in out["silent_members"]] == [
        ({"n": "c"}, "3s"),
        ({"n": "b"}, "2s"),
    ]
    assert out["silent_more"] == 1


def test_no_silent_members_adds_nothing():
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in range(0, 5000, 1000)]
    out = run(meta(end=4000), result(rows, {"a": "a"}))
    assert "silent_members" not in out and "silent_more" not in out


def test_one_failed_minute_of_ten_is_not_lost_coverage():
    """Unknown is neither present nor missing: the other nine buckets read full coverage, no gap,
    and the failure is reported as an unknown span."""
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in range(0, 10_000, 1000) if t != 4000]
    out = run(
        replace(meta(end=9000), failed_spans=[[4000, 4000, "boom"]]), result(rows, {"a": "a"})
    )
    assert out["series"][0]["coverage"] == {"pct": 1.0, "missing": "0s", "longest_gap": None}
    assert len(out["unknown_spans"]) == 1 and "untrusted_data" in out["caveats"]


def test_a_series_unknown_everywhere_has_no_coverage_figure():
    rows = [(t, "a", 1.0, 1.0, 1.0, 1) for t in range(0, 4000, 1000)]
    out = run(replace(meta(end=3000), failed_spans=[[0, 3000, "boom"]]), result(rows, {"a": "a"}))
    assert out["series"][0]["coverage"]["pct"] is None


def test_samples_without_a_value_flag_no_value_not_non_finite():
    """1h9.16: a series of count-only buckets has no mean/min/max (never 0), flags `no_value`
    (absence, cause unknown), and is no gap: its samples arrived."""
    rows = [(t * 1000, "a", None, None, None, 4) for t in range(3)]
    out = run(meta(end=2000), result(rows, {"a": "a"}))
    assert "no_value" in out["caveats"] and "non_finite" not in out["caveats"]
    s = out["series"][0]
    assert (s["mean"], s["min"], s["max"]) == (None, None, None)
    assert s["gaps"] == 0
    assert json.dumps(out, allow_nan=False)


def test_nan_buckets_flag_non_finite_not_no_value():
    nan = float("nan")
    out = run(meta(end=0), result([(0, "a", nan, nan, nan, 4)], {"a": "a"}))
    assert "non_finite" in out["caveats"] and "no_value" not in out["caveats"]


def test_cadence_or_loss_buckets_are_unknown_with_their_reason():
    # 1.1 s samples at a 1 s step skip every 11th bucket at a regular spacing: a cadence or a
    # loss recurring there, counts cannot tell (bucket_state e4v)
    from collections import Counter

    c = Counter(-(-t // STEP) * STEP for t in range(370, 240_000, 1100))
    rows = [(t, "a", 1.0, 1.0, 1.0, n) for t, n in sorted(c.items())]
    out = run(meta(end=240_000), result(rows, {"a": "a"}))
    assert "untrusted_data" in out["caveats"] and "missing_data" not in out["caveats"]
    assert out["unknown_spans"] and {s[2] for s in out["unknown_spans"]} == {"cadence_or_loss"}
    # the source's series interval (1.1 s, configured or learned) says which: a cadence, all OK
    known = replace(meta(end=240_000, resolution=1100),
                    semantics_flags={"series_interval_known": True})  # fmt: skip
    out = run(known, result(rows, {"a": "a"}))
    assert "untrusted_data" not in out["caveats"] and out["unknown_spans"] == []
    # an assumed default says nothing (principle 15)
    out = run(meta(end=240_000, resolution=1100), result(rows, {"a": "a"}))
    assert "untrusted_data" in out["caveats"]
