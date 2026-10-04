import pyarrow as pa

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, Flag, State
from telemetry_nerd.model.caveats import (
    Caveat,
    from_bucket_state,
    interval_caveats,
    runs,
    series_name,
)

STEP = 60_000


def table(rows):
    cols = list(zip(*rows))
    return pa.table(dict(zip(STATE_SCHEMA.names, cols)), schema=STATE_SCHEMA)


def row(t, sid, s, obs=4.0):
    return (t * STEP, sid, obs, 4.0, int(s), 0)


def test_runs_merge_contiguous_buckets_into_time_spans():
    assert runs([2 * STEP, 3 * STEP, 5 * STEP], STEP) == [(STEP, 3 * STEP), (4 * STEP, 5 * STEP)]


def test_series_name_is_promql_style():
    assert series_name({"instance": "a", "job": "x"}) == '{instance="a", job="x"}'
    assert series_name({}) == "{}"


def test_all_ok_gives_no_caveats():
    assert from_bucket_state(table([row(1, "a", State.OK)]), {"a": "a"}, STEP) == []


def test_missing_data_is_per_series_with_spans():
    t = table(
        [
            row(1, "a", State.OK),
            row(2, "a", State.EMPTY, 0),
            row(3, "a", State.PARTIAL, 2),
            row(1, "b", State.OK),
            row(2, "b", State.OK),
            row(3, "b", State.OK),
        ]
    )
    [c] = from_bucket_state(t, {"a": "A", "b": "B"}, STEP)
    assert c.code == "missing_data" and c.severity == "warn" and c.source == "bucket_state"
    assert c.where.series == ["a"] and c.where.spans == [(STEP, 3 * STEP)]
    assert "A" in c.message and "1m" in c.message


def test_unknown_is_one_caveat_with_reason_and_no_series_when_all_affected():
    t = table([row(1, "a", State.UNKNOWN, 0), row(1, "b", State.UNKNOWN, 0)])
    [c] = from_bucket_state(t, {"a": "A", "b": "B"}, STEP, failed=[(STEP, STEP, "timeout")])
    assert c.code == "untrusted_data" and c.where.series is None
    assert "timeout" in c.message


def test_late_born_series_is_info_not_warning():
    t = table([row(1, "a", State.ABSENT, 0), row(2, "a", State.OK)])
    [c] = from_bucket_state(t, {"a": "A"}, STEP)
    assert c.code == "absent_part" and c.severity == "info"


def test_caveat_round_trips_through_json():
    c = Caveat(code="x", message="m", source="validator")
    assert Caveat.model_validate_json(c.model_dump_json()) == c


# --- interval visibility (bead 6nm) ---

RES = 15_000


def _rows(sid, counts, expected, flags=0):
    return [
        (t * STEP, sid, float(c), float(expected), int(State.OK if c else State.EMPTY), flags)
        for t, c in enumerate(counts, start=1)
    ]


def test_coarse_scrape_gets_one_dataset_level_info_caveat():
    t = table(_rows("a", [1] * 5, 1) + _rows("b", [1] * 5, 1) + _rows("c", [4] * 5, 4))
    names = {"a": '{instance="a"}', "b": '{instance="b"}', "c": '{instance="c"}'}
    [c] = interval_caveats(t, names, STEP, RES)
    assert c.code == "interval_differs" and c.severity == "info" and c.source == "bucket_state"
    assert c.where.series == ["a", "b"] and c.where.spans is None
    assert '{instance="a"}, {instance="b"}' in c.message and '{instance="c"}' not in c.message
    assert "every 1m" not in c.message and "every 60s" in c.message and "15s" in c.message


def test_configured_rate_and_jitter_give_no_interval_caveat():
    t = table(_rows("a", [4, 3, 4, 4, 4], 4))
    assert interval_caveats(t, {"a": "A"}, STEP, RES) == []


def test_faster_than_configured_is_also_reported():
    t = table(_rows("a", [8] * 5, 8))
    [c] = interval_caveats(t, {"a": "A"}, STEP, RES)
    assert "every 8s" in c.message  # 7.5s rounds to a whole second


def test_silent_series_and_fake_resolution_are_not_judged():
    silent = table(_rows("a", [0] * 5, 1))
    assert interval_caveats(silent, {"a": "A"}, STEP, RES) == []
    fine_step = table(_rows("a", [1] * 5, 1))
    assert interval_caveats(fine_step, {"a": "A"}, STEP, 4 * STEP) == []


def test_source_filled_is_not_judged():
    t = table(_rows("a", [1] * 5, 1, int(Flag.SOURCE_FILLED)))
    assert interval_caveats(t, {"a": "A"}, STEP, RES) == []


def test_rate_change_caveat_per_series_with_spans():
    f = int(Flag.INTERVAL_CHANGE)
    rows = _rows("a", [4, 4, 4, 4], 2.5) + [
        (t * STEP, "a", 1.0, 2.5, int(State.PARTIAL), f) for t in range(5, 9)
    ]
    t = table(rows + _rows("b", [4] * 8, 4))
    [c] = [c for c in from_bucket_state(t, {"a": '{instance="a"}', "b": "B"}, STEP)
           if c.code == "interval_change"]  # fmt: skip
    assert c.severity == "info" and c.where.series == ["a"]
    assert c.where.spans == [(4 * STEP, 8 * STEP)]
    assert '{instance="a"}' in c.message and "15s → 60s" in c.message


def test_one_sample_per_bucket_says_at_least_the_step():
    t = table(_rows("a", [1] * 5, 1))
    [c] = interval_caveats(t, {"a": "A"}, STEP, RES)
    assert "at least every 60s" in c.message
    t = table(_rows("a", [2] * 5, 2))  # 30s: implied, not a floor
    [c] = interval_caveats(t, {"a": "A"}, STEP, RES)
    assert "about every 30s" in c.message


def test_interval_differs_reports_the_slow_series_own_interval_at_a_finer_step():
    from telemetry_nerd.model.bucket_state import compute
    from telemetry_nerd.model.series import BUCKET_SCHEMA

    s15 = 15_000
    ts = list(range(60_000, 3_600_000 + 1, 60_000))
    buckets = pa.table(
        {"ts_ms": ts, "series_id": ["a"] * len(ts), "avg": [1.0] * len(ts),
         "min": [1.0] * len(ts), "max": [1.0] * len(ts), "count": [1] * len(ts)},
        schema=BUCKET_SCHEMA,
    )  # fmt: skip
    st = compute(buckets, ("a",), start_ms=s15, end_ms=3_600_000, step_ms=s15,
                 resolution_ms=s15, mode="samples")  # fmt: skip
    # configured 15s, real 60s: ratio 4 -> flagged
    [c] = interval_caveats(st, {"a": "A"}, s15, s15) or [None]
    assert c is not None and "about every 60s" in c.message and "15s" in c.message


def test_unknown_cites_only_the_failures_over_its_own_spans():
    t = table(
        [row(i, "a", State.UNKNOWN if i in (2, 3) else State.OK, 0 if i in (2, 3) else 4.0)
         for i in range(1, 8)]
    )  # fmt: skip
    failed = [(2 * STEP, 3 * STEP, "timeout"), (6 * STEP, 7 * STEP, "other window")]
    [c] = from_bucket_state(t, {"a": "A"}, STEP, failed=failed)
    assert "timeout" in c.message and "other window" not in c.message
    # a coarse bucket (end 4 covers (0, 4]) still touches the failure at 2..3
    [c] = from_bucket_state(
        table([row(4, "a", State.UNKNOWN, 0)]), {"a": "A"}, 4 * STEP, failed=failed
    )
    assert "timeout" in c.message and "other window" not in c.message


def test_unknown_without_a_failure_over_it_says_the_source_could_not_tell():
    t = table([row(1, "a", State.UNKNOWN, 0)])
    [c] = from_bucket_state(t, {"a": "A"}, STEP, failed=[(5 * STEP, 6 * STEP, "elsewhere")])
    assert "source could not tell" in c.message and "elsewhere" not in c.message


def test_unknown_spans_stay_per_series():
    # a is unknown at 1, b at 3: neither is marked unknown where only the other one was
    t = table(
        [row(1, "a", State.UNKNOWN, 0), row(2, "a", State.OK), row(3, "a", State.OK),
         row(1, "b", State.OK), row(2, "b", State.OK), row(3, "b", State.UNKNOWN, 0)]
    )  # fmt: skip
    failed = [(STEP, STEP, "first"), (3 * STEP, 3 * STEP, "third")]
    cs = from_bucket_state(t, {"a": "A", "b": "B"}, STEP, failed=failed)
    got = {tuple(c.where.series): (c.where.spans, c.message) for c in cs}
    assert got.keys() == {("a",), ("b",)}
    assert got[("a",)][0] == [(0, STEP)] and "first" in got[("a",)][1]
    assert "third" not in got[("a",)][1]
    assert got[("b",)][0] == [(2 * STEP, 3 * STEP)] and "third" in got[("b",)][1]
    assert "first" not in got[("b",)][1]


def test_series_sharing_unknown_spans_share_one_caveat_and_the_odd_one_has_its_own():
    t = table(
        [row(1, "a", State.UNKNOWN, 0), row(2, "a", State.OK),
         row(1, "b", State.UNKNOWN, 0), row(2, "b", State.OK),
         row(1, "c", State.OK), row(2, "c", State.UNKNOWN, 0)]
    )  # fmt: skip
    failed = [(STEP, STEP, "first"), (2 * STEP, 2 * STEP, "second")]
    cs = from_bucket_state(t, {k: k for k in "abc"}, STEP, failed=failed)
    got = {tuple(c.where.series): c for c in cs}
    assert got.keys() == {("a", "b"), ("c",)}
    assert "first" in got[("a", "b")].message and "second" not in got[("a", "b")].message
    assert "second" in got[("c",)].message and "first" not in got[("c",)].message


def test_unknown_caveat_names_at_most_the_series_cap():
    from telemetry_nerd.model.caveats import MAX_WHERE_SERIES

    n = MAX_WHERE_SERIES + 5
    rows = [row(1, f"s{k:03}", State.UNKNOWN, 0) for k in range(n // 2)]
    rows += [row(1, f"s{k:03}", State.OK) for k in range(n // 2, n + 50)]  # most are fine
    rows += [row(1, f"u{k:03}", State.UNKNOWN, 0) for k in range(n)]
    cs = from_bucket_state(table(rows), {}, STEP)
    assert all(len(c.where.series) <= MAX_WHERE_SERIES for c in cs if c.where.series)
    [big] = [c for c in cs if c.where.series and len(c.where.series) == MAX_WHERE_SERIES]
    assert f"Affects {n + n // 2} series" in big.message


def test_cadence_or_loss_unknown_names_its_reason():
    from collections import Counter

    import pyarrow as pa

    from telemetry_nerd.model.bucket_state import compute
    from telemetry_nerd.model.caveats import CADENCE_REASON, from_bucket_state
    from telemetry_nerd.model.series import BUCKET_SCHEMA

    c = Counter(-(-t // 15_000) * 15_000 for t in range(3_669, 3_600_000, 16_500))
    ts = sorted(c)
    b = pa.table({"ts_ms": ts, "series_id": ["a"] * len(ts), "avg": [1.0] * len(ts),
                  "min": [1.0] * len(ts), "max": [1.0] * len(ts), "count": [c[t] for t in ts]},
                 schema=BUCKET_SCHEMA)  # fmt: skip
    out = compute(b, ("a",), start_ms=15_000, end_ms=3_600_000, step_ms=15_000,
                  resolution_ms=15_000, mode="samples")  # fmt: skip
    [cav] = [x for x in from_bucket_state(out, {"a": "A"}, 15_000) if x.code == "untrusted_data"]
    assert CADENCE_REASON in cav.message
