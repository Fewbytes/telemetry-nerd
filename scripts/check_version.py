#!/usr/bin/env python3
"""Release consistency check (bead ijg.4/ijg.2): pyproject, plugin.json and marketplace.json
versions must agree; with a tag argument (v0.1.0), the tag must equal v<version>.

Stdlib only so CI and tests can run it anywhere:  python scripts/check_version.py [vX.Y.Z]
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def versions() -> dict[str, str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    found = {"pyproject.toml": pyproject, ".claude-plugin/plugin.json": plugin}
    for entry in market["plugins"]:
        found[f"marketplace.json:{entry['name']}"] = entry["version"]
    return found


def check(tag: str | None = None) -> list[str]:
    found = versions()
    errors = []
    expected = found["pyproject.toml"]
    for where, v in found.items():
        if v != expected:
            errors.append(f"{where} version {v!r} != pyproject.toml version {expected!r}")
    if tag is not None and tag != f"v{expected}":
        errors.append(f"git tag {tag!r} != v{expected} (pyproject.toml version)")
    return errors


def main() -> int:
    errors = check(sys.argv[1] if len(sys.argv) > 1 else None)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if not errors:
        print(f"versions consistent: {versions()['pyproject.toml']}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
