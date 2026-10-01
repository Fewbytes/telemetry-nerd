import math

import pytest

from telemetry_nerd.analysis.fraction import fraction_over, wilson

INF = math.inf
LO = [-INF, 0.1, 1.0]
HI = [0.1, 1.0, INF]
C = [60.0, 30.0, 10.0]


def test_exact_at_a_source_edge():
    r = fraction_over(LO, HI, C, 1.0)
    assert (r.exact, r.lo, r.hi, r.above) == (True, 0.1, 0.1, 10.0)
    assert fraction_over(LO, HI, C, 0.1).lo == 0.4


def test_bounded_inside_a_bucket_never_interpolated():
    r = fraction_over(LO, HI, C, 0.5)
    assert (r.exact, r.lo, r.hi, r.bucket) == (False, 0.1, 0.4, (0.1, 1.0))


def test_no_data():
    assert fraction_over([0.0], [1.0], [0.0], 0.5) is None


def test_wilson_interval_brackets_the_proportion_and_narrows_with_n():
    lo, hi = wilson(10, 100)
    assert lo < 0.1 < hi
    lo2, hi2 = wilson(100, 1000)
    assert (hi2 - lo2) < (hi - lo)
    assert wilson(0, 0) == (0.0, 1.0)
    assert wilson(0, 50)[0] == 0.0
    assert wilson(50, 50)[1] == pytest.approx(1.0)
