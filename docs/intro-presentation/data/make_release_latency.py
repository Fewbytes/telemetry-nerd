# /// script
# requires-python = ">=3.11"
# ///
"""Write data/release-latency.csv: checkout latency samples (ms) for two releases.

Lab 4 data for plot mode. Deterministic (seed 2026). v1.5 has the same median as v1.4
but a heavier tail: 4% of its requests take a slow path. A few v1.5 cells are empty
(dropped samples) so attendees see how missing values are counted, not zero-filled.

    uv run docs/intro-presentation/data/make_release_latency.py
"""

import csv
import math
import random
from pathlib import Path

N = 2000
rng = random.Random(2026)


def fast() -> float:
    return rng.lognormvariate(math.log(80), 0.35)


def slow() -> float:
    return rng.lognormvariate(math.log(900), 0.5)


v14 = [fast() if rng.random() > 0.005 else slow() for _ in range(N)]
v15 = [fast() if rng.random() > 0.04 else slow() for _ in range(N)]
for i in rng.sample(range(N), 23):
    v15[i] = None  # type: ignore[call-overload]

out = Path(__file__).with_name("release-latency.csv")
with out.open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["v1_4_ms", "v1_5_ms"])
    for a, b in zip(v14, v15, strict=True):
        w.writerow([f"{a:.1f}", "" if b is None else f"{b:.1f}"])
print(f"wrote {out} ({N} rows)")
