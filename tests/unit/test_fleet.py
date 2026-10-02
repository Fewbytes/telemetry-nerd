"""Fleet analysis statistics (bead lkn.3): spread, outliers, error control, seeded simulation."""

import numpy as np
import pytest

from telemetry_nerd.analysis.fleet import analyse, spread, t_isf, t_sf
from tests.unit.fleet_sim import fleet


def test_student_t_tails_match_tables():
    assert t_isf(0.025, 10) == pytest.approx(2.2281, abs=1e-3)
    assert t_isf(0.005, 3) == pytest.approx(5.8409, abs=1e-3)
    assert t_isf(0.025, 1e7) == pytest.approx(1.95996, abs=1e-3)
    assert t_sf(-2.2281, 10) == pytest.approx(0.975, abs=1e-4)
    # far tail (Bonferroni over thousands of member-steps): t_100 at 1e-7 is ~5.6
    assert 5.4 < t_isf(1e-7, 100) < 5.8


def test_spread_is_over_reporting_members_only_with_n_per_step():
    y = np.array([[1.0, 1.0, np.nan], [2.0, 2.0, np.nan], [3.0, np.nan, np.nan],
                  [4.0, 4.0, 4.0], [100.0, 5.0, 5.0]])  # fmt: skip
    sp = spread(y)
    assert sp.n.tolist() == [5, 4, 2]
    assert sp.alive.tolist() == [5, 5, 5]  # members that stopped reporting are still alive
    assert sp.median[0] == 3.0 and sp.median[1] == pytest.approx(3.0)
    assert np.isnan(sp.median[2])  # 2 members: no median band
    assert sp.q25[0] == 2.0 and np.isnan(sp.q25[1])  # quartiles need n >= 5
    assert np.isnan(sp.q10[0])  # 10/90 need n >= 10
    assert sp.hi[0] == 100.0 and sp.lo[2] == 4.0


def test_three_planted_members_are_found_with_their_kind_and_nothing_else():
    y, planted = fleet(1, m=100, plant=True)
    f = analyse(y)
    assert f.scale == "log" and not f.loo  # 100 > 64 members: full-fleet median / MAD
    kinds = {o.member: o for o in f.outliers}
    assert set(kinds) == set(planted.values()), {m: o.kind for m, o in kinds.items()}
    pers, tran, drift = (kinds[planted[k]] for k in ("persistent", "transient", "drifting"))
    assert pers.kind == "persistent" and pers.direction == "higher" and pers.since_window_start
    # x1.8 planted: the offset (log ratio) and its 99% interval cover log(1.8)
    assert pers.offset_interval[0] < np.log(1.8) < pers.offset_interval[1]
    assert tran.kind == "transient" and not {"level", "change"} & set(tran.fired)
    (ep,) = tran.episodes
    assert 140 <= ep.start <= 146 and 153 <= ep.end <= 160 and ep.sustained
    assert drift.kind == "drifting" and drift.direction == "higher"
    assert drift.since is not None and 30 < drift.since < 200 and not drift.since_window_start
    assert drift.change_interval[0] > 0


def test_small_fleet_uses_exact_leave_one_out_and_still_finds_planted():
    y, _ = fleet(2, m=30, plant=True)
    f = analyse(y)
    assert f.loo
    found = {o.member: o.kind for o in f.outliers}
    assert found == {7: "persistent", 23: "transient", 1: "drifting"}


@pytest.mark.parametrize("m", [30, 100])
def test_homogeneous_fleets_rarely_name_anyone(m):
    """Family-wise 1% design: P(>= 3 alarms in 30 fleets) ~ 0.3% if calibrated."""
    alarms = sum(bool(analyse(fleet(500 + s, m=m)[0]).outliers) for s in range(30))
    assert alarms <= 2


def test_heavy_tailed_noise_is_detected_and_does_not_flood_alarms():
    results = [analyse(fleet(700 + s, m=100, df=4)[0]) for s in range(15)]
    assert all("heavy_tailed_noise" in f.caveats for f in results)
    assert sum(bool(f.outliers) for f in results) <= 2
    # a sustained 12-step episode is still found: the 15-step scale is close to normal
    y, planted = fleet(3, m=100, df=4, plant=True)
    found = {o.member: o.kind for o in analyse(y).outliers}
    assert found.get(planted["transient"]) == "transient"


def test_missing_members_reduce_n_and_are_not_imputed():
    y, planted = fleet(4, m=60, plant=True, missing=0.1)
    f = analyse(y)
    assert f.spread.n.max() <= 60 and f.spread.n.min() < 60
    assert {o.member for o in f.outliers} == set(planted.values())


def test_member_normalisation_compares_shapes_not_sizes():
    y, _ = fleet(5, m=40)
    y[3] *= 3.0  # a bigger instance: same shape, three times the level
    assert [(o.member, o.kind) for o in analyse(y).outliers] == [(3, "persistent")]
    f = analyse(y, normalise="member")
    assert f.outliers == [] and "level" not in f.thresholds.get("tests", [])
    assert np.nanmedian(f.spread.median) == pytest.approx(1.0, abs=0.05)  # ratio to own median


def test_fleet_preconditions():
    with pytest.raises(ValueError, match="needs >= 5"):
        analyse(np.ones((4, 50)))
    y = fleet(6, m=8)[0]
    f = analyse(y)
    assert "too_few_members_for_outliers" in f.caveats and f.outliers == []
    with pytest.raises(ValueError, match="log scale needs"):
        analyse(np.vstack([np.zeros((1, 50)), np.ones((9, 50))]), scale="log")
    assert analyse(np.vstack([np.zeros((1, 50)), np.ones((9, 50))])).scale == "linear"
