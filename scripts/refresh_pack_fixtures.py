"""Re-ground the knowledge packs: fetch live /metadata and rebuild tests/fixtures/packs.

Two bulk calls to Wikimedia (node_exporter; Thanos metadata is non-deterministic, so they are
unioned) and one to Grafana Play (kube-state-metrics, cAdvisor): interactive volume only.
Prints every pack name or relation target that real metadata does not contain.
Run: uv run python scripts/refresh_pack_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from telemetry_nerd.catalog.packs import LoadedPack, builtin_packs
from telemetry_nerd.sources.presets import PLAY, WIKIMEDIA
from telemetry_nerd.sources.promql import USER_AGENT

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "packs"


def fetch_metadata(url: str, calls: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(calls):
            resp = client.get(f"{url}/api/v1/metadata")
            resp.raise_for_status()
            for name, entries in resp.json()["data"].items():
                out.setdefault(name, entries)
    return out


def slice_for(lp: LoadedPack, meta: dict[str, list[dict]]) -> dict[str, list[dict]]:
    keep = {n: e[:1] for n, e in meta.items() if any(en.matches(n) for en in lp.entries)}
    for en in lp.entries:
        for target in en.bounded_by or []:
            if target in meta:
                keep.setdefault(target, meta[target][:1])
    return keep


def report(lp: LoadedPack, meta: dict[str, list[dict]]) -> None:
    print(lp.pack.name, "exact names not in real metadata:", sorted(lp.exact_names() - meta.keys()))
    targets = {t for en in lp.entries for t in en.bounded_by or []}
    print("  bounded_by targets not in real metadata:", sorted(targets - meta.keys()))
    for en in lp.entries:
        if en.match and not any(en.matches(n) for n in meta):
            print("  regex matches nothing:", en.match)


def main() -> None:
    packs = {lp.pack.name: lp for lp in builtin_packs().packs}
    OUT.mkdir(parents=True, exist_ok=True)
    for pack, url, name in (
        ("node_exporter", WIKIMEDIA.url, "wikimedia_node_metadata.json"),
        ("kubernetes", PLAY.url, "play_k8s_metadata.json"),
    ):
        meta = fetch_metadata(url, calls=2 if url == WIKIMEDIA.url else 1)
        report(packs[pack], meta)
        keep = slice_for(packs[pack], meta)
        (OUT / name).write_text(json.dumps(keep, indent=0, sort_keys=True) + "\n")
        print(f"{name}: {len(keep)} entries")


if __name__ == "__main__":
    main()
