#!/usr/bin/env python3
"""Run the unit suite K times with different PYTHONHASHSEED values (bead zek0.1).

Every run must pass: a failure that depends on hash order is a flake and a P0 bug. Runs all K
even after a failure and lists which seeds failed, so the failing seed can be replayed with
`PYTHONHASHSEED=<seed> uv run pytest tests/unit`.
"""

import os
import subprocess
import sys

SEEDS = [1, 2, 3, 7, 42, 1337, 31337, 99991, 123456, 4294967295]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    failed = []
    for i in range(n):
        seed = SEEDS[i] if i < len(SEEDS) else 1000 + i
        print(f"=== unit run {i + 1}/{n} PYTHONHASHSEED={seed}", flush=True)
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        rc = subprocess.run(
            ["uv", "run", "pytest", "tests/unit", "-q", "-p", "no:cacheprovider"],
            env=env,
            check=False,
        ).returncode
        if rc:
            failed.append(seed)
    if failed:
        print(f"FAILED seeds: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
