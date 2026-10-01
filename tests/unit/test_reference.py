import pytest

from telemetry_nerd.analysis.reference import WEEK_MS, reference_window

H, M = 3_600_000, 60_000
START, END = 100 * H, 101 * H  # buckets at START..END inclusive, each covering (ts - step, ts]


def buckets(s, e, step):
    return set(range(s, e + 1, step))


def test_previous_is_adjacent_equal_length_and_never_overlaps():
    r = reference_window(START, END, M, "previous")
    assert (r.mode, r.shift_ms) == ("previous", H + M)
    assert (
        r.end_ms + M == START
    )  # last reference bucket ends one step before the first panel bucket
    assert len(buckets(r.start_ms, r.end_ms, M)) == len(buckets(START, END, M)) == 61
    assert not buckets(r.start_ms, r.end_ms, M) & buckets(START, END, M)
    assert r.label == "previous window"


def test_week_shifts_by_seven_days_on_the_same_grid():
    r = reference_window(START + 7 * 24 * H, END + 7 * 24 * H, 5 * M, "week")
    assert (r.start_ms, r.end_ms, r.shift_ms) == (START, END, WEEK_MS)
    assert r.label == "same window last week"


def test_week_refused_when_it_would_overlap_or_misalign():
    with pytest.raises(ValueError, match="longer than a week"):
        reference_window(0, 8 * 24 * H, H, "week")
    with pytest.raises(ValueError, match="does not divide a week"):
        reference_window(START, END, 11 * M, "week")


def test_profile_waits_for_the_catalog_and_bad_input_is_refused():
    with pytest.raises(ValueError, match="2as.7"):
        reference_window(START, END, M, "profile")
    with pytest.raises(ValueError, match="unknown reference"):
        reference_window(START, END, M, "yesterday")
    with pytest.raises(ValueError, match="window"):
        reference_window(END, START, M, "previous")
