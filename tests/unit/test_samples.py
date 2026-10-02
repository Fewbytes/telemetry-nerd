import pytest

from telemetry_nerd.analysis.samples import pool, scan_series

RAMP = [float(i) for i in range(30)]


def test_counter_with_a_reset():
    s = scan_series([*range(0, 50, 5), 1.0, 6.0, 11.0])  # 10 samples up, reset, 2 up
    assert (s.resets, s.small_decreases, s.negatives) == (1, 0, 0)
    assert s.increases == 11 and s.decreases == 1


def test_gauge_wobble_is_small_decreases_not_resets():
    s = scan_series([10, 9, 11, 10.5, 12, 11, 13, 12, 14, 13, 15])
    assert s.resets == 0 and s.small_decreases == 5


def test_a_decrease_to_under_half_is_a_reset_and_exactly_half_is_not():
    assert scan_series([10, 4.99]).resets == 1
    assert scan_series([10, 5]).small_decreases == 1


def test_negatives_integrality_constancy_and_nulls():
    s = scan_series([-1, 0, None, float("nan"), 2])
    assert s.n == 3 and s.negatives == 1 and s.integral and not s.constant
    assert scan_series([3, 3, 3]).constant
    assert not scan_series([1.5, 2]).integral
    empty = scan_series([None, float("inf")])
    assert empty.n == 0 and empty.min is None


def test_pooling_votes_only_series_with_enough_samples():
    counter = scan_series(RAMP)
    wobble = scan_series([5, 4, 6, 5, 7, 6, 8, 7, 9, 8, 10])
    tiny = scan_series([1, 0.9])  # 2 samples: does not vote
    p = pool([counter, wobble, tiny])
    assert (p.series, p.voting) == (3, 2) and p.gauge_voters == 1
    assert p.gauge_like  # half of the voters wobble


def test_verdicts():
    assert pool([scan_series(RAMP)]).verdict == "counter-like"
    assert pool([scan_series([2.0] * 20)]).verdict == "constant"
    assert pool([scan_series([5, 4, 6, 5, 7, 6, 8, 7, 9, 8, 10])]).verdict == "gauge-like"
    assert pool([scan_series([1, 2, 1.9])]).verdict == "inconclusive"
    assert pool([]).verdict == "no data"


@pytest.mark.parametrize(
    ("vals", "grows", "nonneg"),
    [
        (RAMP, True, True),
        ([*RAMP, 3.0], False, True),  # a reset: not grows-only
        ([3.0, -1.0, *RAMP], False, False),
        (RAMP[:8], True, False),  # too few samples to claim non-negativity
    ],
)
def test_grows_only_and_nonnegative(vals, grows, nonneg):
    p = pool([scan_series(vals)])
    assert p.grows_only is grows and p.nonnegative is nonneg


def test_counter_like_needs_growth_and_no_small_decreases():
    assert not pool(
        [scan_series([1, 1, 1, 2, 2, 3, 3, 3, 3, 3, 3])]
    ).counter_like  # only 2 increases
    assert pool([scan_series(RAMP)]).counter_like
    assert not pool([scan_series([*RAMP[:15], 13.0, *RAMP[16:]])]).counter_like  # small decrease
