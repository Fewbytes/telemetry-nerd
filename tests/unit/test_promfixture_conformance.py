"""The e2e fixture engine against real Prometheus (bead y7hb).

tests/fixtures/missing-data/prometheus/ holds Prometheus 3.15 query_range responses over the
controlled history of scripts/missing_data_lab.py (`seed_series`: gappy gauges, a counter with a
gap and a restart, a single sample, a histogram with a partial bucket). Rebuilding that history
in the fixture store and replaying every recorded syn_* query must give the same points.
"""

import importlib.util
import json
import math
from pathlib import Path

import pytest

from telemetry_nerd.devtools.promfixture.engine import Engine
from telemetry_nerd.devtools.promfixture.server import parse_step, parse_time
from telemetry_nerd.devtools.promfixture.store import Store

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "tests/fixtures/missing-data/prometheus"
_spec = importlib.util.spec_from_file_location(
    "missing_data_lab", ROOT / "scripts/missing_data_lab.py"
)
assert _spec and _spec.loader
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)


def _recorded() -> list[Path]:
    out = []
    for f in sorted(DIR.glob("prom__*.json")):
        d = json.loads(f.read_text())
        req = d.get("request") or {}
        query = (req.get("params") or {}).get("query", "")
        if "syn_" in query and d.get("status") == 200 and req.get("path") == "/api/v1/query_range":
            out.append(f)
    return out


@pytest.fixture(scope="module")
def engine() -> Engine:
    samples = json.loads((DIR / "prom__gapfill_i15_samples.json").read_text())
    start = round(float(samples["body"]["data"]["result"][0]["values"][0][0]) * 1000)
    store = Store()
    for labels, points in lab.seed_series(start + 47 * 60_000):
        store.add(labels, points)
    return Engine(store)


def _points(rows) -> dict[tuple, dict[int, float]]:
    return {
        tuple(sorted(r["metric"].items())): {
            round(float(t) * 1000): float(v) for t, v in r["values"]
        }
        for r in rows
    }


def test_there_are_recordings_to_compare():
    assert len(_recorded()) >= 20


@pytest.mark.parametrize("path", _recorded(), ids=lambda p: p.stem)
def test_engine_matches_recorded_prometheus(engine, path):
    d = json.loads(path.read_text())
    p = d["request"]["params"]
    want = _points(d["body"]["data"]["result"])
    rows = engine.range(
        p["query"], parse_time(p["start"]), parse_time(p["end"]), parse_step(p["step"])
    )
    got = {tuple(sorted(lb.items())): dict(zip(ts, vs, strict=True)) for lb, ts, vs in rows}
    assert want.keys() == got.keys(), p["query"]
    for key, w in want.items():
        g = got[key]
        assert w.keys() == g.keys(), (p["query"], key)
        for t, v in w.items():
            assert math.isclose(v, g[t], rel_tol=1e-9, abs_tol=1e-9) or (
                math.isnan(v) and math.isnan(g[t])
            ), (p["query"], key, t, v, g[t])
