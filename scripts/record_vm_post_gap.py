# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Record VictoriaMetrics post-gap fixtures for rollups beyond increase/rate (bead telemetry-nerd-mj0).

  uv run scripts/record_vm_post_gap.py [http://127.0.0.1:8428]

Writes its own synthetic series under a unique metric name per run (the VM persists data), then
queries each rollup at window = step (15 s) and window = 5 x step (75 s). Counter: +15 per 15 s
sample (truth 1/s), 10 min hole from +600 s. Gauge: ramp +2 per sample, same hole.
Re-running overwrites the vm__pg_* fixtures (new metric name, new timestamps).
Fixtures: tests/fixtures/missing-data/victoriametrics/vm__pg_<kind>_<func>_w<seconds>.json
(same shape as record_missing_data.py; the unique metric name is in each request).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "tests/fixtures/missing-data/victoriametrics"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8428"
STEP = 15
HOLE = (600, 1200)  # seconds from s0 without samples
SPAN = 1800
FUNCS = ("rate", "irate", "delta", "idelta", "increase_pure", "rate_over_sum", "deriv", "increase")


def straddle(names: dict, s0: int) -> None:
    """Window start inside the hole: the first value is still computed from the pre-window sample."""
    for func in ("increase", "delta"):
        params = {
            "query": f"{func}({names['counter']}[{STEP}s])",
            "start": f"{s0 + 800:.3f}",
            "end": f"{s0 + 1500:.3f}",
            "step": f"{STEP}s",
            "nocache": "1",
        }
        r = httpx.get(f"{BASE}/api/v1/query_range", params=params, timeout=30)
        r.raise_for_status()
        rec = {
            "claim": f"pg_straddle_{func}_w{STEP}",
            "source": "vm",
            "note": f"counter +15 per sample, hole {HOLE[0]}-{HOLE[1]}s after t0={s0}; "
            "query window starts at +800 s, inside the hole",
            "request": {"path": "/api/v1/query_range", "params": params},
            "status": r.status_code,
            "body": r.json(),
        }
        (OUT / f"vm__pg_straddle_{func}_w{STEP}.json").write_text(
            json.dumps(rec, separators=(",", ":")) + "\n"
        )


def main() -> None:
    run = int(time.time())
    s0 = (run - 3600) // STEP * STEP
    names = {"counter": f"pg{run}_counter_total", "gauge": f"pg{run}_gauge"}
    lines = []
    for i in range(SPAN // STEP + 1):
        t = i * STEP
        if HOLE[0] <= t < HOLE[1]:
            continue
        ts = (s0 + t) * 1000
        lines.append(f"{names['counter']} {15 * (i + 1)} {ts}")
        lines.append(f"{names['gauge']} {100 + 2 * i} {ts}")
    httpx.post(
        f"{BASE}/api/v1/import/prometheus", content="\n".join(lines).encode()
    ).raise_for_status()
    httpx.get(f"{BASE}/internal/force_flush", timeout=30).raise_for_status()
    time.sleep(2)
    straddle(names, s0)
    for kind, name in names.items():
        for func in FUNCS:
            for w in (STEP, 5 * STEP):
                params = {
                    "query": f"{func}({name}[{w}s])",
                    "start": f"{s0 + 300:.3f}",
                    "end": f"{s0 + SPAN:.3f}",
                    "step": f"{STEP}s",
                    "nocache": "1",
                }
                r = httpx.get(f"{BASE}/api/v1/query_range", params=params, timeout=30)
                r.raise_for_status()
                rec = {
                    "claim": f"pg_{kind}_{func}_w{w}",
                    "source": "vm",
                    "note": f"{kind} sample every {STEP}s, hole {HOLE[0]}-{HOLE[1]}s after t0={s0}; "
                    f"{'+15 per sample' if kind == 'counter' else '100+2i ramp'}",
                    "request": {"path": "/api/v1/query_range", "params": params},
                    "status": r.status_code,
                    "body": r.json(),
                }
                p = OUT / f"vm__pg_{kind}_{func}_w{w}.json"
                p.write_text(json.dumps(rec, separators=(",", ":")) + "\n")
                print(p.name, r.status_code)


if __name__ == "__main__":
    main()
