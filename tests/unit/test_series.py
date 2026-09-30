from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    empty_result,
    labels_json,
    series_id,
)


def test_series_id_is_stable_and_order_independent():
    a = series_id("vm", {"job": "api", "instance": "a"})
    b = series_id("vm", {"instance": "a", "job": "api"})
    assert a == b
    assert len(a) == 16
    int(a, 16)  # hex


def test_series_id_depends_on_source():
    assert series_id("vm", {"x": "1"}) != series_id("prom", {"x": "1"})


def test_labels_json_is_canonical():
    assert labels_json({"b": "2", "a": "1"}) == '{"a":"1","b":"2"}'


def test_empty_result_has_schemas():
    r = empty_result()
    assert r.buckets.schema == BUCKET_SCHEMA
    assert r.series.schema == SERIES_SCHEMA
    assert r.buckets.num_rows == 0
