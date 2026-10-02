# /// script
# requires-python = ">=3.12"
# ///
"""Calibrate binding verdicts (bead czt.4) by seeded simulation.

Run from the repo root: `uv run python scripts/calibrate_verdicts.py [--seeds 400]`.

Synthetic RED (rate, errors, duration) and USE (utilization, saturation) windows from
tests/unit/verdict_sim.py: 1-min steps, 1 h windows, 4 previous windows as the reference. Per
scenario: share of seeds with any role flagged (quiet: the family-wise false-alarm rate at
alpha 5%), the share where the planted role is flagged, its onset error (median |onset - truth|
in minutes) and interval coverage, and for the two-signal scenario how often the order is
claimed correctly / wrongly / left as simultaneous (overlapping onset intervals).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry_nerd.analysis.verdicts import judge_roles
from tests.unit.verdict_sim import (
    STEP_MS,
    Scenario,
    cycles,
    red_inputs,
    ts,
    use_inputs,
)

T = ts()
SCENARIOS = [
    ("quiet RED", "red", None, {}),
    ("quiet USE", "use", None, {}),
    ("error burst x10, min 30-37", "red", Scenario(error_burst=(30, 37)), {"errors": 30}),
    ("latency median x1.6 from min 20", "red", Scenario(latency_shift=20), {"duration": 20}),
    (
        "error burst x3, min 30-37",
        "red",
        Scenario(error_burst=(30, 37), error_factor=3),
        {"errors": 30},
    ),
    (
        "latency median x1.2 from min 20",
        "red",
        Scenario(latency_shift=20, latency_factor=1.2),
        {"duration": 20},
    ),
    ("request rate x0.6 from min 30", "red", Scenario(rate_drop=30), {"rate": 30}),
    (
        "latency x1.6 from 20, then errors x10 at 40-47",
        "red",
        Scenario(latency_shift=20, error_burst=(40, 47)),
        {"duration": 20, "errors": 40},
    ),
    (
        "saturation: util 0.97 min 20-40, queue x5 from 22",
        "use",
        Scenario(saturation=(20, 40)),
        {"utilization": 20, "saturation": 22},
    ),
]


def onset_min(ms: int | None) -> float:
    return np.nan if ms is None else (ms - (T[0] - STEP_MS)) / STEP_MS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=400)
    args = ap.parse_args()
    print(
        "| scenario | any role flagged | planted role(s) flagged | onset error (min) | onset interval covers | order right / wrong / simultaneous |"
    )
    print("|---|---|---|---|---|---|")
    for name, kind, sc, truth in SCENARIOS:
        anyf = 0
        hit = dict.fromkeys(truth, 0)
        err: list[float] = []
        cover = []
        right = wrong = simul = 0
        for seed in range(args.seeds):
            cs = cycles(10_000 + seed, sc)
            res, _, order = judge_roles(
                red_inputs(cs) if kind == "red" else use_inputs(cs), T, STEP_MS
            )
            anyf += any(r.judgement.status == "changed" for r in res.values())
            for role, t in truth.items():
                j = res[role].judgement
                if j.status != "changed":
                    continue
                hit[role] += 1
                if j.onset and j.onset.at_ms is not None:
                    err.append(abs(onset_min(j.onset.at_ms) - t))
                    cover.append(onset_min(j.onset.lo_ms) <= t <= onset_min(j.onset.hi_ms))
            if len(truth) == 2 and all(res[r].judgement.status == "changed" for r in truth):
                a, b = sorted(truth, key=truth.get)  # a truly moved first
                ca, cb = (next(i for i, c in enumerate(order.clusters) if r in c) for r in (a, b))
                if ca == cb:
                    simul += 1
                elif ca < cb:
                    right += 1
                else:
                    wrong += 1
        n = args.seeds
        hits = ", ".join(f"{r} {100 * h / n:.1f}%" for r, h in hit.items()) or "-"
        oe = f"{np.median(err):.1f}" if err else "-"
        cv = f"{100 * np.mean(cover):.1f}%" if cover else "-"
        od = f"{right} / {wrong} / {simul}" if len(truth) == 2 else "-"
        print(f"| {name} | {100 * anyf / n:.1f}% | {hits} | {oe} | {cv} | {od} |")


if __name__ == "__main__":
    main()
