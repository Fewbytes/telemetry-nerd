"""Behaviour groups of heterogeneous fleets (bead lkn.10): split, explain, analyse per group."""

import numpy as np
import pytest

from telemetry_nerd.analysis.fleet import analyse
from telemetry_nerd.analysis.fleet_clusters import adjusted_rand, analyse_groups, cluster
from tests.unit.fleet_sim import fleet


def two_sizes(seed: int, small: int = 30, m: int = 100):
    """30/70 fleet: members 0..29 are twice the size; planted persistent (7) and transient (23)
    in the small group, drifting (61) in the large one."""
    y, planted = fleet(seed, m=m, plant=True)
    y[:small] *= 2.0
    labels = [
        {"pod": f"api-{i:03d}", "size": "small" if i < small else "large", "zone": f"z{i % 3}"}
        for i in range(m)
    ]
    return y, planted, labels


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_two_sizes_split_explained_and_outliers_found_within_groups(seed):
    y, planted, labels = two_sizes(seed)
    f = analyse(y)
    assert "many_outliers" in f.caveats  # the 30 small members all look persistent
    g = analyse_groups(y, f, labels)
    assert g is not None and len(g.groups) == 2
    sets = sorted((set(x.members) for x in g.groups), key=len)
    assert sets == [set(range(30)), set(range(30, 100))]
    best = g.explained_by[0]
    assert best["label"] == "size" and best["ari"] > 0.95
    assert all(e["label"] not in ("pod", "zone") for e in g.explained_by)
    found = {}
    for grp in g.groups:
        for o in grp.fleet.outliers:
            found[grp.members[o.member]] = (o.kind, len(grp.members))
    assert found[planted["persistent"]] == ("persistent", 30)
    assert found[planted["transient"]] == ("transient", 30)
    assert found[planted["drifting"]][0] in ("drifting", "shifted")
    assert found[planted["drifting"]][1] == 70  # placed by its label (behaviour is ambiguous)
    assert set(found) == set(planted.values())


@pytest.mark.parametrize("kw", [{}, {"het": 0.3}, {"df": 3}])
def test_clustering_does_not_invent_groups(kw):
    """Gated on many_outliers (which homogeneous fleets almost never trip), and the split test
    itself rejects one-group fleets even ungated, including a wide unimodal spread of levels."""
    splits = 0
    for s in range(12):
        y, _ = fleet(40_000 + s, m=100, **kw)
        f = analyse(y)
        assert analyse_groups(y, f, [{"pod": str(i)} for i in range(100)]) is None
        splits += cluster(f.d, f.tested).k > 1
    assert splits <= 1


def test_lone_deviant_members_are_outliers_not_groups():
    y, _ = fleet(3, m=40, plant=True)
    f = analyse(y)
    c = cluster(f.d, f.tested)
    assert c.k == 1  # a split would leave < 5 members on one side


def test_adjusted_rand_ignores_labels_that_name_every_member():
    a = [0] * 10 + [1] * 10
    assert adjusted_rand(a, a) == pytest.approx(1.0)
    assert abs(adjusted_rand(a, list(range(20)))) < 0.05
    assert adjusted_rand(a, ["x"] * 5 + ["y"] * 5 + ["x"] * 5 + ["y"] * 5) < 0.1


def test_unclustered_fleet_keeps_the_fleet_level_result():
    y, planted = fleet(1, m=100, plant=True)
    f = analyse(y)
    assert analyse_groups(y, f, [{"pod": str(i)} for i in range(100)]) is None
    assert {o.member for o in f.outliers} == set(planted.values())


def test_groups_are_analysed_at_split_alpha():
    y, _, labels = two_sizes(4)
    f = analyse(y)
    g = analyse_groups(y, f, labels)
    assert g is not None
    k = len(g.groups)
    for grp in g.groups:
        ref = analyse(y[grp.members], scale=f.scale, alpha=0.01 / k)
        assert grp.fleet.thresholds["member"] == pytest.approx(ref.thresholds["member"])
        assert np.isfinite(grp.level)
