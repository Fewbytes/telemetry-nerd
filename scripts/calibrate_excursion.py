# /// script
# requires-python = ">=3.12"
# ///
"""Calibrate analyze's excursion test (bead 7f15) by seeded simulation.

Run from the repo root: `uv run python scripts/calibrate_excursion.py [--trials 4000]`.
Short series (the shape round-5 shipping had: 24 points at 2 m, default baseline the first
half), noise of several kinds. False alarms: share of no-change series in which each model's
p < 0.01 (the label rests on the cautious one: design <= 1%). Power: share of series with a
planted episode (k marginal sigmas for `width` steps inside the judged half, then back)
labelled special cause.
"""

from __future__ import annotations

import argparse
import sys
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry_nerd.analysis.autocorr import positions
from telemetry_nerd.analysis.excursion import excursion

STEP = 120_000


def ar1(rng, n, phi):
    e = rng.normal(size=n + 50)
    x = np.zeros_like(e)
    for i in range(1, e.size):
        x[i] = phi * x[i - 1] + e[i]
    return x[50:] * np.sqrt(1 - phi * phi)


def noise(rng, n, kind):
    if kind == "white":
        return rng.normal(size=n)
    if kind == "ar0.5":
        return ar1(rng, n, 0.5)
    if kind == "ar0.8":
        return ar1(rng, n, 0.8)
    if kind == "window2":  # rate(x[4m]) at 2m: neighbours share half their data
        e = rng.normal(size=n + 1)
        return (e[1:] + e[:-1]) / np.sqrt(2)
    if kind == "t3":
        return rng.standard_t(3, size=n) / np.sqrt(3)
    if kind == "lognormal0.5":
        return np.exp(0.5 * rng.normal(size=n))
    if kind == "lognormal1":
        return np.exp(rng.normal(size=n))
    if kind == "ar0.5-lognormal0.5":
        return np.exp(0.5 * ar1(rng, n, 0.5))
    raise ValueError(kind)


KINDS = ["white", "ar0.5", "ar0.8", "window2", "t3", "lognormal0.5", "lognormal1",
         "ar0.5-lognormal0.5"]  # fmt: skip


def rates(rng, n, kind, trials, k=0.0, width=4):
    ts = np.arange(n, dtype=np.int64) * STEP
    pos = positions(ts, STEP)
    base = np.arange(n) < n // 2
    b = c = 0
    for _ in range(trials):
        y = noise(rng, n, kind)
        if k:
            sd = float(np.std(noise(rng, 4000, kind)))
            at = n // 2 + (n - n // 2 - width) // 2
            y[at : at + width] += k * sd
        ex = excursion(ts, pos, y, base)
        if ex is None:
            continue
        b += ex.p < 0.01
        c += ex.p_cautious < 0.01
    return b / trials, c / trials


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=4000)
    a = ap.parse_args()
    print("| noise | n | false alarms: baseline model | cautious (label) |")
    print("|---|---|---|---|")
    for n in (24, 31, 60):
        for kind in KINDS:
            rng = np.random.default_rng(zlib.crc32(f"{n}{kind}".encode()))
            fb, fc = rates(rng, n, kind, a.trials)
            print(f"| {kind} | {n} | {fb:.2%} | {fc:.2%} |")
    print()
    print("| noise | n | episode | power: baseline model | cautious (label) |")
    print("|---|---|---|---|---|")
    for n in (24, 31):
        for kind in ("white", "window2", "ar0.5", "lognormal0.5"):
            for k in (10, 20, 30, 100):
                rng = np.random.default_rng(zlib.crc32(f"{n}{kind}{k}".encode()))
                pb, pc = rates(rng, n, kind, a.trials // 4, k=k)
                print(f"| {kind} | {n} | {k} sd x 4 steps | {pb:.1%} | {pc:.1%} |")


if __name__ == "__main__":
    main()
