"""Fleets of mostly exact zeros (bead gzrz): sparse error counters per service.

The RED errors role over span metrics is an error ratio per service that is exactly 0 almost
everywhere. That made the pooled sigma 0 (no z), the excursion scale ratio 0/0
(ZeroDivisionError in _excursion_bars, which took show_binding down) and the Gumbel tail fit
anchor on a point mass at 0.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from telemetry_nerd.analysis.fleet import (
    analyse,
    constant_reference,
    gumbel_bars,
    loo_deviations,
    median_ratio_iid,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fleet" / "red_errors_demo.json"


def _demo():
    d = json.loads(FIXTURE.read_text())
    return np.array(d["values"]), d["members"]


def test_the_demo_shape_names_the_three_failing_services_and_nothing_else():
    y, names = _demo()
    f = analyse(y)  # raised ZeroDivisionError before gzrz
    assert f.untested == [] and f.departing == []  # every member judged, none dropped
    named = {names[o.member]: o for o in f.outliers}
    assert set(named) == {"payment", "checkout", "frontend"}
    for o in named.values():
        assert o.kind == "transient" and o.direction == "higher"
        # the 14:53-14:57 failure (steps 212..231 of the 15 s grid from 14:00)
        assert [(e.start, e.end) for e in o.episodes] == [(212, 231)]
    # payment's two single-error minutes are not an outlier on their own
    assert "degenerate_excursion_scale" in f.caveats
    assert f.thresholds["episode_scale_from_model"] == 1.0
    assert all(math.isfinite(v) for v in f.thresholds.values())


def test_an_all_zero_fleet_is_tested_and_has_no_outliers():
    f = analyse(np.zeros((13, 240)))
    assert f.tested == list(range(13)) and f.untested == [] and f.outliers == []
    assert "no_spread" in f.caveats and "too_few_members_for_outliers" not in f.caveats
    assert np.all(f.z == 0)


def test_a_single_departing_member_is_named_not_judged_and_not_dropped():
    y = np.zeros((13, 240))
    y[4, 100:110] = 0.5
    f = analyse(y)
    assert f.departing == [4] and "constant_reference" in f.caveats
    assert 4 in f.untested and f.tested == [i for i in range(13) if i != 4]
    assert "members_skipped" not in f.caveats  # it has data: it is not 'too little data'
    assert f.outliers == []
    # the others, whose reference includes member 4, have a scale and z 0 where they agree
    assert np.all(f.z[np.arange(13) != 4] == 0)


def test_two_departing_members_are_judged_against_each_other():
    y = np.zeros((13, 240))
    y[4, 100:110] = 0.5
    y[7, 30:32] = 0.01
    f = analyse(y)
    assert f.departing == [] and f.untested == []


def test_constant_reference_marks_only_members_whose_others_never_vary():
    y = np.zeros((6, 20))
    y[2, 5] = 1.0
    c = constant_reference(y)
    assert c[2].all() and not c[[0, 1, 3, 4, 5]].any()
    d, z, _, _ = loo_deviations(y)
    assert z[2, 0] == 0 and math.isnan(z[2, 5]) and d[2, 5] == 1.0


def test_zero_local_spread_takes_the_window_wide_sigma():
    y = np.zeros((10, 60))
    y[0, 50] = 1.0  # one value away from the others: the steps far from it have no local spread
    _, z, cnt, _ = loo_deviations(y)
    assert np.all(np.isfinite(z[1:])) and np.all(z[1:] == 0)
    assert np.all(cnt[1:, :10] > 0)  # the window-wide pool backs those steps


@pytest.mark.parametrize(("w", "want"), [(1, 1.0), (5, 0.5337), (15, 0.3184)])
def test_median_ratio_iid_is_the_scale_of_a_median_of_w_normals(w, want):
    assert median_ratio_iid(w) == pytest.approx(want, abs=1e-3)


def test_median_ratio_iid_matches_simulation():
    rng = np.random.default_rng(3)
    m = np.median(rng.standard_normal((200_000, 5)), axis=1)
    got = np.median(np.abs(m)) / 0.6744897501960817
    assert median_ratio_iid(5) == pytest.approx(got, rel=0.01)


def test_gumbel_bars_fit_no_tail_to_a_pool_of_zero_peaks():
    peaks = np.array([0.0] * 9 + [3.0, 4.0, 5.0])
    pool = np.ones(12, bool)
    assert np.all(gumbel_bars(peaks, pool, 1e-4) == -math.inf)
    tied = np.full(12, 2.0)
    assert np.all(gumbel_bars(tied, pool, 1e-4) == -math.inf)
    rng = np.random.default_rng(1)
    ok = gumbel_bars(np.abs(rng.standard_normal(12)) + 3, pool, 1e-4)
    assert np.all(np.isfinite(ok)) and np.all(ok > 3)
