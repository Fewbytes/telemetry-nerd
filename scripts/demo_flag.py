# /// script
# requires-python = ">=3.11"
# ///
"""List / read / toggle flagd feature flags of the local OTel demo (deploy/demo).

  uv run scripts/demo_flag.py list                  # flag, state, default variant, variants
  uv run scripts/demo_flag.py get paymentFailure    # what flagd *serves* (OFREP evaluation)
  uv run scripts/demo_flag.py set paymentFailure 50%   # variant name; or off/on
  uv run scripts/demo_flag.py off                   # every flag back to its "off" variant

`set` rewrites deploy/demo/flagd/demo.flagd.json in place (flagd reloads within a couple of seconds); `get` asks flagd over OFREP (http://127.0.0.1:8016), so it
shows the live value. Exits 1 on an unknown flag/variant.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

FLAG_FILE = Path(__file__).resolve().parent.parent / "deploy/demo/flagd/demo.flagd.json"
OFREP = os.environ.get("DEMO_OFREP_URL", "http://127.0.0.1:8016")


def load() -> dict:
    return json.loads(FLAG_FILE.read_text())


def save(doc: dict) -> None:
    # In place, one write(): flagd watches the file's inode, and a write-then-rename replace
    # (tried first) made it log "error restoring watcher" and keep the old flags.
    data = (json.dumps(doc, indent=2) + "\n").encode()
    with open(FLAG_FILE, "r+b") as f:
        f.write(data)
        f.truncate()


def evaluate(name: str) -> dict:
    req = urllib.request.Request(
        f"{OFREP}/ofrep/v1/evaluate/flags/{name}",
        data=b'{"context": {}}',
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:200]}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    doc = load()
    flags = doc["flags"]
    if cmd == "list":
        for n, f in flags.items():
            print(
                f"{n:30} {f['state']:8} default={f['defaultVariant']:8} variants={list(f['variants'])}"
            )
        return 0
    if cmd == "get" and len(argv) == 2:
        print(json.dumps(evaluate(argv[1])))
        return 0
    if cmd == "off":
        for f in flags.values():
            f["defaultVariant"] = "off" if "off" in f["variants"] else f["defaultVariant"]
        save(doc)
        return 0
    if cmd == "set" and len(argv) == 3:
        name, variant = argv[1], argv[2]
        if name not in flags:
            print(f"unknown flag {name!r}; try `list`", file=sys.stderr)
            return 1
        if variant not in flags[name]["variants"]:
            print(
                f"{name}: variant {variant!r} not in {list(flags[name]['variants'])}",
                file=sys.stderr,
            )
            return 1
        flags[name]["defaultVariant"] = variant
        save(doc)
        return 0
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
