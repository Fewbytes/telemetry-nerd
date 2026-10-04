"""Text Claude and the user read uses the glossary's terms (docs/glossary.md, bead iep5).

Scans skills, commands, the MCP tool descriptions and the UI sources for phrases the glossary
retires. "scrape interval" is allowed on a line that names the Prometheus-specific case
("scraped"); JSON keys and identifiers (`scrape_interval_ms`) never match (no space).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FILES = [
    *ROOT.glob("skills/**/*.md"),
    *ROOT.glob("commands/*.md"),
    ROOT / "src/telemetry_nerd/mcp/server.py",
    ROOT / "src/telemetry_nerd/analysis/sources.py",
    *(p for p in ROOT.glob("ui/src/**/*.ts") if not p.name.endswith(".test.ts")),
    *ROOT.glob("ui/src/**/*.svelte"),
]

# phrase -> the glossary term to write instead
BANNED = {
    r"scrape intervals?": "series interval",
    r"source interval": "series interval",
    r"native resolution": "series interval",
    r"source resolution": "series interval",
    r"rate windows?": "query window",
    r"display step": "display bucket",
}
ALLOWED_ON_LINE = re.compile(r"scraped", re.IGNORECASE)  # the Prometheus-specific case


def test_retired_interval_terms_are_not_used():
    assert len(FILES) > 20, "the scan found too few files: paths moved?"
    hits = []
    for path in FILES:
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for phrase, instead in BANNED.items():
                if re.search(rf"\b{phrase}\b", line, re.IGNORECASE) and not (
                    phrase.startswith("scrape") and ALLOWED_ON_LINE.search(line)
                ):
                    hits.append(f"{path.relative_to(ROOT)}:{no}: {phrase!r} -> {instead!r}")
    assert not hits, "use the docs/glossary.md terms:\n" + "\n".join(hits)
