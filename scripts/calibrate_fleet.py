# /// script
# requires-python = ">=3.12"
# ///
"""Calibrate fleet outlier detection (bead lkn.3) by seeded simulation.

Run from the repo root: `uv run python scripts/calibrate_fleet.py [--seeds 200]`.
False alarms: share of homogeneous fleets in which ANY member is named (family-wise, design 1%).
Detection: share of fleets with planted persistent / transient / drifting members in which each is
named with the right kind (drifting may come out as `shifted`: both mean the change test fired).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry_nerd.analysis.fleet import analyse
from tests.unit.fleet_sim import fleet

SCENARIOS = [
    # name, kwargs
    ("M=10 AR(0.6) normal", {"m": 10}),
    ("M=30 AR(0.6) normal", {"m": 30}),
    ("M=100 AR(0.6) normal", {"m": 100}),
    ("M=300 AR(0.6) normal", {"m": 300}),
    ("M=100 white normal", {"m": 100, "phi": 0.0}),
    ("M=100 AR(0.9) normal", {"m": 100, "phi": 0.9}),
    ("M=100 AR(0.6) t(4)", {"m": 100, "df": 4}),
    ("M=30 AR(0.6) t(4)", {"m": 30, "df": 4}),
    ("M=100 AR(0.6) 10% missing", {"m": 100, "missing": 0.1}),
    ("M=100 no heterogeneity", {"m": 100, "het": 0.0}),
    ("M=100 AR(0.6) t(3)", {"m": 100, "df": 3}),
]
KIND_OK = {
    "persistent": {"persistent"},
    "transient": {"transient"},
    "drifting": {"drifting", "shifted"},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--only", default="", help="run scenarios whose name contains this")
    args = ap.parse_args()
    seeds = args.seeds
    print(
        "| scenario | false alarm (any member) | by test level/change/spike/episode/long | detect persistent/transient/drifting |"
    )
    print("|---|---|---|---|")
    for name, kw in [s for s in SCENARIOS if args.only in s[0]]:
        fa, by = 0, {"level": 0, "change": 0, "spike": 0, "episode": 0, "long_episode": 0}
        hits = {"persistent": 0, "transient": 0, "drifting": 0}
        for seed in range(seeds):
            y, _ = fleet(10_000 + seed, **kw)
            f = analyse(y)
            fa += bool(f.outliers)
            for t in by:
                by[t] += any(t in o.fired for o in f.outliers)
            if kw["m"] < 24:
                continue
            y, planted = fleet(20_000 + seed, plant=True, **kw)
            g = analyse(y)
            found = {o.member: o.kind for o in g.outliers}
            for kind, member in planted.items():
                hits[kind] += found.get(member) in KIND_OK[kind]
        det = " / ".join(f"{hits[k] / seeds:.1%}" for k in hits) if kw["m"] >= 24 else "-"
        tests = " / ".join(f"{by[t] / seeds:.1%}" for t in by)
        print(f"| {name} | {fa / seeds:.1%} | {tests} | {det} |", flush=True)


if __name__ == "__main__":
    main()
