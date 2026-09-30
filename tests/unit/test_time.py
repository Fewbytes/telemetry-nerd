import pytest

from telemetry_nerd.model.time import (
    TimeRange,
    format_duration,
    iso,
    parse_duration,
    parse_time,
)

NOW = 1_700_000_000_000


def test_parse_duration_units():
    assert parse_duration("250ms") == 250
    assert parse_duration("15s") == 15_000
    assert parse_duration("5m") == 300_000
    assert parse_duration("2h") == 7_200_000
    assert parse_duration("1d") == 86_400_000
    assert parse_duration("1w") == 604_800_000


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("5 minutes")


@pytest.mark.parametrize("text", ["250ms", "15s", "90s", "1m", "2h", "1d", "1w"])
def test_format_duration_roundtrip(text):
    assert format_duration(parse_duration(text)) == text


def test_parse_time_forms():
    assert parse_time("now", NOW) == NOW
    assert parse_time("now-1h", NOW) == NOW - 3_600_000
    assert parse_time("1699990000000", NOW) == 1_699_990_000_000
    assert parse_time("2023-11-14T22:13:20+00:00", NOW) == 1_700_000_000_000


def test_parse_time_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        parse_time("2023-11-14T22:13:20", NOW)


def test_time_range_rejects_empty():
    with pytest.raises(ValueError):
        TimeRange(10, 10)


def test_time_range_align_expands_to_step_multiples():
    assert TimeRange(61_000, 179_000).align(60_000) == TimeRange(60_000, 180_000)
    assert TimeRange(60_000, 180_000).align(60_000) == TimeRange(60_000, 180_000)


def test_iso():
    assert iso(1_700_000_000_000) == "2023-11-14T22:13:20+00:00"
