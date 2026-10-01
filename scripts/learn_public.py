"""Run T0 learning against a public preset source and print what the catalog learned.

Interactive volume only: discover() is ~4 listing calls (names, metadata x<=3, labels, tsdb).
Run: uv run python scripts/learn_public.py play [--record DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from collections import Counter
from pathlib import Path

import httpx

from telemetry_nerd.catalog.rules import facts_from_claims
from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.presets import PRESETS
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.replay import RecordingTransport


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("preset", choices=sorted(PRESETS))
    ap.add_argument("--record", type=Path, help="save raw responses here (offline fixtures)")
    ap.add_argument("--probe", nargs="*", default=[], help="metrics to print in detail")
    ap.add_argument("--show-conflicts", type=int, default=0)
    args = ap.parse_args()

    transport = httpx.AsyncHTTPTransport()
    if args.record:
        transport = RecordingTransport(transport, args.record)
    client = httpx.AsyncClient(transport=transport)
    spec = PRESETS[args.preset]
    src = PromQLSource.from_spec(spec, client=client)

    with tempfile.TemporaryDirectory() as tmp:
        svc = build_service(Settings(data_dir=Path(tmp), source_url="http://127.0.0.1:9"))
        svc.sources.attach(spec.name, src, spec)
        try:
            out = await svc.learn(spec.name)
        finally:
            await client.aclose()
        print({k: v for k, v in out.items() if k != "source"})
        entries = svc.ws.catalog_list(spec.name)
        by_field = Counter(f for e in entries for f in e.fields)
        print("fields claimed:", dict(by_field.most_common()))
        print(
            "units:",
            dict(
                Counter(e.fields["unit"].value for e in entries if "unit" in e.fields).most_common(
                    8
                )
            ),
        )
        print(
            "types:",
            dict(
                Counter(e.fields["type"].value for e in entries if "type" in e.fields).most_common()
            ),
        )
        print(
            "unit origins:",
            dict(Counter(e.fields["unit"].origin for e in entries if "unit" in e.fields)),
        )
        fams = [
            e.metric
            for e in entries
            if "histogram_family" in e.fields and e.fields["histogram_family"].value == [e.metric]
        ]
        print(f"native histogram families: {len(fams)} e.g. {fams[:5]}")
        conflicted = [e for e in entries if e.conflicts()]
        print("conflicts:", len(conflicted))
        for e in conflicted[: args.show_conflicts]:
            for f, losers in e.conflicts().items():
                win = e.fields[f]
                print(
                    f"  {e.metric}.{f}: {win.origin}={win.value!r} vs {[(c.origin, c.value) for c in losers]}"
                )
        for m in args.probe:
            e = svc.ws.catalog_entry(spec.name, m)
            print(m, {f: (c.value, c.origin, c.confidence) for f, c in e.fields.items()})
            print(
                "   facts:", facts_from_claims(e.claims.get("unit", []) + e.claims.get("type", []))
            )


asyncio.run(main())
