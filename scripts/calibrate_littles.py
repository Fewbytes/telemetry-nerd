"""Little's law check: seeded calibration on the discrete-event M/M/c simulation (czt.2, 9fd).

  uv run python scripts/calibrate_littles.py [--quick] [--out build/calibrate_littles.json]

Rows of the validation table in docs/superpowers/specs/2026-10-02-littles-law-design.md: false
alarms (verdict not consistent) and pointwise 95% coverage of 1 on consistent traffic, detection
of a gauge offset, a load spike's peak window (special cause: beyond the envelope or promoted)
with a completions and an arrivals counter, and promotions where there must be none. Seeds are
fixed, so a run on another commit is directly comparable.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telemetry_nerd.analysis.littles import SPECIAL, check
from tests.unit.littles_sim import simulate, substeps

LOAD = [(0.0, 2.0), (1200.0, 3.7), (2400.0, 2.0)]
SPIKE = [(0.0, 2.0), (1500.0, 5.0), (1800.0, 2.0)]


def consistent(seed: int, lam: float, c: int) -> dict:
    r = check(substeps(simulate(seed, rates=[(0.0, lam)], c=c)), k=20)
    cov = [w.ci95[0] <= 1 <= w.ci95[1] for w in r.windows if w.ci95]
    return {"alarm": r.verdict != "consistent", "covered": sum(cov), "windows": len(cov),
            "promoted": bool(r.promoted)}  # fmt: skip


def load_steps(seed: int) -> dict:
    r = check(substeps(simulate(seed, rates=LOAD)), k=20, arrivals="arrivals")
    return {"alarm": r.verdict != "consistent", "promoted": bool(r.promoted),
            "special": any(w.source == SPECIAL for w in r.windows)}  # fmt: skip


def gauge_offset(seed: int, lam: float, c: int, factor: float) -> dict:
    m = simulate(seed, rates=[(0.0, lam)], c=c)
    m["gauge"] = m["gauge"] * factor
    r = check(substeps(m), k=20)
    return {"detected": r.verdict == "L_high"}


def confined(seed: int, factor: float, offset: bool) -> dict:
    """The gauge counts `factor` x more for exactly one window (a deploy, say), off peak."""
    m = simulate(seed, rates=[(0.0, 6.0)], c=10)
    m["gauge"] = m["gauge"].astype(float)
    m["gauge"][60:80] *= factor
    r = check(substeps(m), k=20, offset=offset)
    return {"detected": any(t["source"] == SPECIAL for t in r.transient),
            "inconsistent": r.verdict == "inconsistent_in_windows"}  # fmt: skip


OVERLOAD = [(0.0, 2.0), (1200.0, 4.2), (2100.0, 2.0)]  # rho 1.05 for 15 min


def overload(seed: int, offset: bool) -> dict:
    r = check(substeps(simulate(seed, rates=OVERLOAD)), k=20, arrivals="arrivals", offset=offset)
    special = (
        any(w.source == SPECIAL for w in r.windows)
        or bool(r.promoted)
        or any(t["source"] == SPECIAL for t in r.transient)
    )
    return {"special": special, "promoted": bool(r.promoted)}


def short_spike(seed: int, counter: str, offset: bool) -> dict:
    """rho 1.5 for 90 s (M/M/4, lambda 2 -> 6) at a seeded position in minutes 20-40: an
    episode (build + drain, ~3 min) shorter than the 5-minute window, anywhere on the grid.
    Detected: a special-cause window (either grid) overlapping it; stray: one elsewhere."""
    import random

    a = 1200.0 + random.Random(seed).uniform(0, 1200)
    m = simulate(seed, rates=[(0.0, 2.0), (a, 6.0), (a + 90, 2.0)], counter=counter)
    r = check(substeps(m), k=20, arrivals=counter, offset=offset)
    spans = [(w.start_ms, w.end_ms) for w in r.windows if w.source == SPECIAL]
    spans += [((t.get("block") or r.windows[t["index"]]).start_ms,
               (t.get("block") or r.windows[t["index"]]).end_ms)
              for t in r.transient + r.promoted if t.get("grid")]  # fmt: skip
    lo, hi = a * 1000, (a + 300) * 1000
    hit = [x for x in spans if x[0] < hi and x[1] > lo]
    return {"detected": bool(hit), "stray": len(hit) < len(spans)}


def spike(seed: int, counter: str) -> dict:
    r = check(substeps(simulate(seed, rates=SPIKE, counter=counter)), k=20, arrivals=counter)
    peak = r.windows[5]  # sub-steps 15 s, windows of 5 min: t = 1500..1800 s
    return {"peak_special": peak.source == SPECIAL, "promoted": bool(r.promoted),
            "transient_peak": any(t["index"] == 5 for t in r.transient)}  # fmt: skip


def run(cases: list[tuple]) -> list[dict]:
    with ProcessPoolExecutor() as ex:
        futs = [ex.submit(f, *a) for f, *a in cases]
        return [f.result() for f in futs]


def rate(rows: list[dict], key: str) -> str:
    k = sum(bool(r[key]) for r in rows)
    return f"{k}/{len(rows)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="a third of the seeds")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "calibrate_littles.json")
    a = ap.parse_args()
    q = (lambda n: n // 3) if a.quick else (lambda n: n)
    table: dict[str, dict] = {}

    def row(name: str, cases: list[tuple], *keys: str) -> None:
        rows = run(cases)
        out = {k: rate(rows, k) for k in keys}
        if "covered" in rows[0]:
            cov = sum(r["covered"] for r in rows) / max(1, sum(r["windows"] for r in rows))
            out["coverage"] = f"{100 * cov:.1f}%"
        table[name] = out
        print(f"{name}: {out}", flush=True)

    row("consistent lambda=2 c=4", [(consistent, s, 2.0, 4) for s in range(q(150))],
        "alarm", "promoted")  # fmt: skip
    row("consistent lambda=9.5 c=10", [(consistent, 1000 + s, 9.5, 10) for s in range(q(150))],
        "alarm", "promoted")  # fmt: skip
    row("consistent lambda=19 c=25", [(consistent, 2000 + s, 19.0, 25) for s in range(q(50))],
        "alarm", "promoted")  # fmt: skip
    row("consistent lambda=0.3", [(consistent, 3000 + s, 0.3, 4) for s in range(q(150))],
        "alarm", "promoted")  # fmt: skip
    row("load steps rho .5-.925-.5", [(load_steps, 4000 + s) for s in range(q(75))],
        "alarm", "promoted", "special")  # fmt: skip
    for lam, c in ((9.5, 10), (2.0, 4)):
        for f in (1.05, 1.1, 1.2):
            row(f"gauge x{f} at lambda={lam}",
                [(gauge_offset, 5000 + s, lam, c, f) for s in range(q(75))], "detected")  # fmt: skip
    for f in (1.3, 1.6):
        for off in (False, True):
            row(f"gauge x{f} in one window, offset grid {'on' if off else 'off'}",
                [(confined, 7000 + s, f, off) for s in range(q(75))],
                "detected", "inconsistent")  # fmt: skip
    for off in (False, True):
        row(f"overload rho 1.05 15 min, arrivals counter, offset grid {'on' if off else 'off'}",
            [(overload, 8000 + s, off) for s in range(q(75))], "special", "promoted")  # fmt: skip
    for counter in ("completions", "arrivals"):
        for off in (False, True):
            row(f"90 s spike rho 1.5 anywhere, {counter} counter, offset grid "
                f"{'on' if off else 'off'}",
                [(short_spike, 9000 + s, counter, off) for s in range(q(75))],
                "detected", "stray")  # fmt: skip
    for counter in ("completions", "arrivals"):
        row(f"spike rho 1.25 5 min, {counter} counter",
            [(spike, 6000 + s, counter) for s in range(q(75))],
            "peak_special", "transient_peak", "promoted")  # fmt: skip
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(table, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
