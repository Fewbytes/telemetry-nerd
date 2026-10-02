# /// script
# requires-python = ">=3.12"
# ///
"""Calibrate the seasonal comparison (beads lkn.2 / lkn.7 / lkn.8) by seeded simulation.

Run from the repo root: `uv run python scripts/calibrate_seasonal.py [--seeds 1000] [--only ...]`.

Table 1 (lkn.2): weekly load, multiplicative noise (white or AR(0.7) within the window), optional
per-cycle level jitter, 96-point windows, 1w scheme, k cycles; the held-out "now" is a normal
cycle. Band coverage = share of now's points inside the 90% band; level PI coverage = now's level
inside the 90% normal interval; false alarms per detector and any.

Table 2 (lkn.8): atypical-cycle exclusion. Normal references (false exclusion of any cycle) and a
weekday window against 7 daily references that include Saturday and Sunday (both must go).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry_nerd.analysis.seasonal import Cycle, compare, cycle_shifts

H, M, DAY = 3_600_000, 60_000, 86_400_000
MON = 1_790_553_600_000  # Monday 2026-09-28 00:00 UTC
STEP = 5 * M


def load(t_ms: np.ndarray, rng, jitter: float, phi: float, sd: float = 0.08) -> np.ndarray:
    """Weekday business-hours peak, low weekends; multiplicative noise (AR(phi), marginal sd)."""
    t = np.asarray(t_ms)
    hour = (t % DAY) / H
    dow = ((t // DAY) + 3) % 7  # 0 = Monday
    daily = 1 + 2.0 * np.exp(-(((hour - 14) / 3.5) ** 2))
    weekend = np.where(dow >= 5, 0.35, 1.0)
    level = 100 * (1 + (daily - 1) * weekend)
    e = rng.normal(0, sd, t.size)
    if phi:
        x = np.empty_like(e)
        x[0] = e[0]
        for i in range(1, e.size):
            x[i] = phi * x[i - 1] + np.sqrt(1 - phi * phi) * e[i]
        e = x
    return level * jitter * np.exp(e)


def sim(
    seed: int, k: int, phi: float, jitter_sd: float, scheme: str = "1w", n: int = 96, start=None
):
    rng = np.random.default_rng(seed)
    if start is None:
        start = MON + int(rng.integers(0, 7)) * DAY + int(rng.integers(0, 24)) * H
    grid = start + np.arange(n) * STEP
    cyc = []
    for j, sh in enumerate(cycle_shifts(start, scheme, k, "UTC", n * STEP, STEP), 1):
        vals = load(grid - sh, rng, float(np.exp(rng.normal(0, jitter_sd))), phi)
        cyc.append(Cycle(scheme, j, sh, start - sh, vals))
    now = load(grid, rng, float(np.exp(rng.normal(0, jitter_sd))), phi)
    return now, cyc


ROWS = [(3, 0.0, 0.0), (3, 0.7, 0.05), (4, 0.0, 0.0), (4, 0.0, 0.05), (4, 0.7, 0.05),
        (6, 0.0, 0.05), (6, 0.7, 0.0)]  # fmt: skip


def table_lkn2(seeds: int) -> None:
    print("| k | noise | jitter | band coverage (90%) | level 90% PI coverage | false alarms: level / extremes / outside / any | atypical excl. |")  # fmt: skip
    print("|---|---|---|---|---|---|---|")
    for k, phi, jit in ROWS:
        ins = tot = lv_in = 0
        fa = {"level": 0, "extremes": 0, "outside": 0, "any": 0}
        excl = 0
        for s in range(seeds):
            now, cyc = sim(20_000 + s, k, phi, jit)
            c = compare(now, cyc, STEP)
            ok = ~np.isnan(c.lo)
            ins += int(np.sum((now[ok] >= c.lo[ok]) & (now[ok] <= c.hi[ok])))
            tot += int(ok.sum())
            lv_in += c.level.normal[0] <= c.level.ratio <= c.level.normal[1]
            fa["level"] += c.level.flagged
            fa["extremes"] += c.extremes.flagged
            fa["outside"] += c.outside.flagged
            fa["any"] += c.verdict == "unusual"
            excl += any(w == "atypical" for _, w in c.excluded)
        noise = "AR(0.7)" if phi else "white"
        f = " / ".join(
            f"{100 * fa[x] / seeds:.1f}%" for x in ("level", "extremes", "outside", "any")
        )
        print(f"| {k} | {noise} | {jit:.0%} | {ins / tot:.3f} | {lv_in / seeds:.3f} | {f} | {100 * excl / seeds:.1f}% |")  # fmt: skip


def table_lkn8(seeds: int) -> None:
    print()
    print("| scenario | excluded as atypical | verdict unusual |")
    print("|---|---|---|")
    # a weekday business-hours window against the previous 7 days (Sat + Sun among them), and a
    # holiday week (x0.6) among 5 weekly references
    rows = [("1d k=7, Wed 09-17, jitter 5% (Sat+Sun in refs)", "1d", 7, 2, 0.05, None),
            ("1d k=7, Wed 09-17, jitter 2% (Sat+Sun in refs)", "1d", 7, 2, 0.02, None),
            ("1d k=7, Tue 09-17, jitter 5% (Sat+Sun in refs)", "1d", 7, 1, 0.05, None),
            ("1w k=5, Wed 09-17, jitter 5%, one holiday week x0.6", "1w", 5, 2, 0.05, 0.6)]  # fmt: skip
    for name, scheme, k, day, jit, holiday in rows:
        hit = wrong = unusual = 0
        for s in range(seeds):
            now, cyc = sim(30_000 + s, k, 0.0, jit, scheme, 96, MON + day * DAY + 9 * H)
            if holiday:
                cyc[2].values = cyc[2].values * holiday
                bad = {cyc[2].j}
            else:
                bad = {cy.j for cy in cyc if ((cy.start_ms // DAY) + 3) % 7 >= 5}
            c = compare(now, cyc, STEP)
            gone = {cy.j for cy, w in c.excluded if w == "atypical"}
            hit += bad <= gone
            wrong += bool(gone - bad)
            unusual += c.verdict == "unusual"
        print(f"| {name} | all atypical {100 * hit / seeds:.1f}%, a normal one {100 * wrong / seeds:.1f}% | {100 * unusual / seeds:.1f}% |")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=1000)
    ap.add_argument("--only", default="", help="lkn2 | lkn8")
    args = ap.parse_args()
    if args.only in ("", "lkn2"):
        table_lkn2(args.seeds)
    if args.only in ("", "lkn8"):
        table_lkn8(args.seeds)


if __name__ == "__main__":
    main()
