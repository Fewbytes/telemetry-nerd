import pyarrow as pa

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State
from telemetry_nerd.model.caveats import Caveat, from_bucket_state, runs, series_name

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
