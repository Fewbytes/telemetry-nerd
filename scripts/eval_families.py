"""Score name-template detection on the hand-labelled Wikimedia sample (or on live names).

uv run python scripts/eval_families.py            # fixtures in tests/fixtures/families
uv run python scripts/eval_families.py --live     # also detect over every live Wikimedia name

Labels (labels.json) say, for 120 sampled names, whether the name encodes a dimension (an
identifier such as a dag, wiki, repository or host) and which text that is. They were written by the
author of the detector, from the names alone; they are not an independent review.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from telemetry_nerd.catalog.families import detect
from telemetry_nerd.catalog.families_eval import evaluate

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "families"


def live_names() -> list[str]:
    import httpx

    from telemetry_nerd.sources.presets import WIKIMEDIA
    from telemetry_nerd.sources.promql import USER_AGENT

    with httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}) as client:
        return client.get(f"{WIKIMEDIA.url}/api/v1/label/__name__/values").json()["data"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="one polite fetch of all Wikimedia names")
    args = ap.parse_args()
    names = json.loads((FIX / "wikimedia_names_sample.json").read_text())
    labels = json.loads((FIX / "labels.json").read_text())
    s = evaluate(detect(names), labels)
    print(f"labelled sample: {s.labelled} names, {s.positives} name-encoded dimensions")
    print(
        f"  precision {s.precision:.3f}  recall {s.recall:.3f}  dimension agreement {s.dimension_agreement:.3f}"
    )
    print("  false positives:", s.false_positives)
    print("  missed:", s.missed)
    if args.live:
        t = time.monotonic()
        all_names = live_names()
        d = detect(all_names)
        print(
            f"live: {len(all_names)} names -> {len(d.families)} families covering {len(d.assignment)} "
            f"names ({time.monotonic() - t:.1f}s)"
        )
        for f in sorted(d.families, key=lambda f: -f.members)[:10]:
            print(f"  {f.members:>7} {f.template[:100]}")


if __name__ == "__main__":
    main()
