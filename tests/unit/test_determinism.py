"""Output never depends on polars thread count, hash seeds or source row order (zek0.3, n3sv).

polars group_by / unique order is random per process and float aggregation may be partitioned by
thread; Python string hashing (set order) changes with PYTHONHASHSEED; a source or cache owes no
row order. `determinism_ops` runs analyze, fleet, compare_seasonal, the query and distribution
summaries and the panel payloads over a fleet with tied members; each run is a fresh process."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ops(threads: int, hashseed: int, shuffle: int | None) -> dict:
    env = {**os.environ, "POLARS_MAX_THREADS": str(threads), "PYTHONHASHSEED": str(hashseed)}
    cmd = [sys.executable, "-m", "tests.unit.determinism_ops"]
    if shuffle is not None:
        cmd += ["--shuffle", str(shuffle)]
    p = subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    assert p.returncode == 0, p.stderr[-4000:]
    return json.loads(p.stdout)


def _diff(a, b, path: str = "$") -> list[str]:
    """Paths where two JSON documents differ (key order and list order included)."""
    if isinstance(a, dict) and isinstance(b, dict):
        head = [f"{path}: keys {list(a)} != {list(b)}"] if list(a) != list(b) else []
        return head + [d for k in a if k in b for d in _diff(a[k], b[k], f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list):
        head = [f"{path}: len {len(a)} != {len(b)}"] if len(a) != len(b) else []
        return head + [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=False))
            for d in _diff(x, y, f"{path}[{i}]")
        ]
    nan = isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b)
    if a == b or nan:  # NaN == NaN here
        return []
    return [f"{path}: {a!r} != {b!r}"]


@pytest.mark.slow
def test_ops_are_identical_across_threads_hash_seeds_and_row_order():
    base = _ops(threads=1, hashseed=1, shuffle=None)
    for threads, hashseed, shuffle in ((4, 2, None), (8, 3, 7), (2, 4, 11)):
        got = _ops(threads, hashseed, shuffle)
        diffs = _diff(base, got)
        assert not diffs, f"threads={threads} hashseed={hashseed} shuffle={shuffle}:\n" + "\n".join(
            diffs[:30]
        )


def test_diff_sees_order_and_values():
    assert _diff({"a": [1, 2]}, {"a": [1, 2]}) == []
    assert _diff({"a": [1, 2]}, {"a": [2, 1]}) == ["$.a[0]: 1 != 2", "$.a[1]: 2 != 1"]
    assert _diff({"a": 1, "b": 2}, {"b": 2, "a": 1}) == ["$: keys ['a', 'b'] != ['b', 'a']"]
    assert _diff({"x": float("nan")}, {"x": float("nan")}) == []
