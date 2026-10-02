# /// script
# requires-python = ">=3.12"
# ///
"""Calibrate fleet outlier detection (beads lkn.3, lkn.14) and behaviour groups (lkn.10) by
seeded simulation.

Run from the repo root: `uv run python scripts/calibrate_fleet.py [--seeds 300] [--jobs 8]`.
False alarms: share of homogeneous fleets in which ANY member is named (family-wise, design 1%).
Detection: share of fleets with planted persistent / transient / drifting members in which each is
named with the right kind (drifting may come out as `shifted`: both mean the change test fired).
Groups: share of fleets split into behaviour groups (homogeneous: must stay ~0), and for a 30/70
two-size fleet the share split exactly right with the planted members named in their own group.
"""

from __future__ import annotations

import argparse
import sys
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry_nerd.analysis.fleet import analyse
from telemetry_nerd.analysis.fleet_clusters import analyse_groups
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
TESTS = ("level", "change", "spike", "episode", "long_episode")


def labels_of(m: int, small: int = 0) -> list[dict]:
    return [{"pod": f"p{i}", "size": "small" if i < small else "large"} for i in range(m)]


def run_fleet(job: tuple[dict, int]) -> tuple[bool, set[str], dict[str, bool], bool]:
    kw, seed = job
    y, _ = fleet(10_000 + seed, **kw)
    f = analyse(y)
    split = analyse_groups(y, f, labels_of(kw["m"])) is not None
    hits: dict[str, bool] = {}
    if kw["m"] >= 24:
        y, planted = fleet(20_000 + seed, plant=True, **kw)
        found = {o.member: o.kind for o in analyse(y).outliers}
        hits = {kind: found.get(member) in KIND_OK[kind] for kind, member in planted.items()}
    return bool(f.outliers), {t for o in f.outliers for t in o.fired}, hits, split


def run_two_sizes(seed: int) -> tuple[bool, bool, bool]:
    """30/70 fleet (members 0..29 twice the size), planted persistent 7 + transient 23 (small)
    and drifting 61 (large): split exactly, planted named in their group, nothing else named."""
    y, planted = fleet(30_000 + seed, m=100, plant=True)
    y[:30] *= 2.0
    f = analyse(y)
    g = analyse_groups(y, f, labels_of(100, small=30))
    if g is None:
        return False, False, False
    exact = sorted(len(x.members) for x in g.groups) == [30, 70] and set(
        min(g.groups, key=lambda x: len(x.members)).members
    ) == set(range(30))
    found = {}
    for grp in g.groups:
        for o in grp.fleet.outliers:
            found[grp.members[o.member]] = o.kind
    named = all(found.get(m) in KIND_OK[k] for k, m in planted.items())
    return exact, named, set(found) == set(planted.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--only", default="", help="run scenarios whose name contains this")
    args = ap.parse_args()
    seeds = args.seeds
    print(
        "| scenario | false alarm (any member) | by test level/change/spike/episode/long "
        "| detect persistent/transient/drifting | split into groups |"
    )
    print("|---|---|---|---|---|")
    with Pool(args.jobs) as pool:
        for name, kw in [s for s in SCENARIOS if args.only in s[0]]:
            res = pool.map(run_fleet, [(kw, s) for s in range(seeds)])
            fa = sum(r[0] for r in res)
            tests = " / ".join(f"{sum(t in r[1] for r in res) / seeds:.1%}" for t in TESTS)
            det = (
                " / ".join(f"{sum(r[2][k] for r in res) / seeds:.1%}" for k in KIND_OK)
                if kw["m"] >= 24
                else "-"
            )
            split = sum(r[3] for r in res) / seeds
            print(f"| {name} | {fa / seeds:.1%} | {tests} | {det} | {split:.1%} |", flush=True)
        if args.only in "two sizes 30/70":
            res = pool.map(run_two_sizes, range(seeds))
            exact, named, only = (sum(r[i] for r in res) / seeds for i in range(3))
            print(
                f"\nTwo sizes 30/70: split exactly {exact:.1%}, planted named in their group "
                f"{named:.1%}, nothing else named {only:.1%}"
            )


if __name__ == "__main__":
    main()
