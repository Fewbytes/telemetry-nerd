"""fleet heatmap payload: sort order, clipping, gaps, row cap (bead lkn.11)."""

import numpy as np
import pytest

from telemetry_nerd.analysis.fleet import analyse
from telemetry_nerd.core import fleet_payloads
from telemetry_nerd.model.bucket_state import State
from tests.unit.fakes import make_service
from tests.unit.fleet_sim import fleet
from tests.unit.test_fleet_service import STEP, T0, put


def test_heat_rows_pin_outliers_at_the_edges_and_keep_gaps_null():
    y, _ = fleet(21, m=60, plant=True, missing=0.02)
    y[5, :20] = np.nan  # member 5 appears late
    f = analyse(y)
    names = [f"m{i}" for i in range(60)]
    h = fleet_payloads.heat_rows(f, names)
    ids = [r["id"] for r in h["rows"]]
    assert len(ids) == 60 and h["rows_total"] == 60
    outl = {names[o.member]: o for o in f.outliers}
    assert outl
    higher = [
        i
        for i, r in enumerate(h["rows"])
        if r["rank"] is not None and outl[r["id"]].direction == "higher"
    ]
    assert higher == list(range(len(higher)))  # higher outliers lead
    row5 = h["rows"][ids.index("m5")]
    assert row5["z"][:20] == [None] * 20 and row5["first"] == 20
    assert all(v is None or abs(v) <= fleet_payloads.Z_CLIP for r in h["rows"] for v in r["z"])
    middle = [r for r in h["rows"] if r["rank"] is None]
    meds = [float(np.nanmedian([v for v in r["z"] if v is not None] or [0])) for r in middle]
    assert meds == sorted(meds, reverse=True)


def test_heat_rows_cap_keeps_every_outlier(monkeypatch):
    monkeypatch.setattr(fleet_payloads, "MAX_HEAT_ROWS", 20)
    y, _ = fleet(22, m=60, plant=True)
    f = analyse(y)
    h = fleet_payloads.heat_rows(f, [f"m{i}" for i in range(60)])
    assert len(h["rows"]) == 20 and h["rows_total"] == 60
    assert sum(r["rank"] is not None for r in h["rows"]) == len(f.outliers)


def test_show_fleet_panel_carries_heat(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, fleet(23, m=40, plant=True)[0])
    svc.fleet(d)
    shown = svc.show(d, "who is off?", mark="fleet")
    data = svc.panel_data(shown.panel.id, 800)
    assert len(data["heat"]["rows"]) == 40 and len(data["heat"]["rows"][0]["z"]) == 288


def test_cloud_is_normalised_by_alive_so_silent_members_thin_it(tmp_path):
    svc = make_service(tmp_path)
    y, _ = fleet(24, m=40)
    y[:3, 200:] = np.nan  # three members go silent two-thirds in; they stay alive
    d = put(svc, y)
    shown = svc.show(d, "who is off?", mark="fleet")
    cloud = svc.panel_data(shown.panel.id, 800)["cloud"]
    sums = [sum(col) for col in cloud["density"]]
    assert sums[100] == pytest.approx(1.0, abs=0.01)  # everyone reports
    assert sums[250] == pytest.approx(37 / 40, abs=0.01)  # 37 of the 40 alive
    assert cloud["silent_n"][100] == 0 and cloud["silent_n"][250] == 3
    silent = {m["id"]: m["steps"] for m in cloud["silent_members"]}
    assert silent == {f"pod=api-{i:03d}": 88 for i in range(3)} and cloud["silent_more"] == 0
    rug = cloud["rug"]
    at = rug["ts"].index(T0 + 250 * STEP)
    assert (rug["alive"][at], rug["reporting"][at]) == (40, 37)
    assert rug["state"][at] == int(State.PARTIAL) and rug["state"][100] == int(State.OK)
