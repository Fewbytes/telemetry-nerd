"""Registry of public Prometheus-compatible sources (see public-sources.toml).

Entries are data only: nothing connects until the user (or Claude) asks by name, e.g.
`source_connect("grafana-play")`. Politeness settings travel into the SourceSpec so the
adapter's gate enforces them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from telemetry_nerd.model.time import parse_duration
from telemetry_nerd.sources.spec import NAME_PATTERN, Politeness, SourceSpec

REGISTRY_FILE = Path(__file__).with_name("public-sources.toml")

Backend = Literal["prometheus", "thanos", "mimir", "victoriametrics"]


class PublicSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME_PATTERN)
    url: str
    backend: Backend
    flavor: Literal["prometheus", "victoriametrics"] = "prometheus"
    resolution_ms: int = 15_000
    politeness: Politeness
    max_range: str  # longest range for one query, e.g. "7d"
    suggested_use: str
    fixtures: str | None = None  # dir under tests/fixtures with recorded responses

    @property
    def max_range_s(self) -> int:
        return parse_duration(self.max_range) // 1000

    def to_spec(self) -> SourceSpec:
        return SourceSpec(
            name=self.name,
            url=self.url,
            flavor=self.flavor,
            resolution_ms=self.resolution_ms,
            politeness=self.politeness,
        )

    def describe(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "backend": self.backend,
            "flavor": self.flavor,
            "resolution_ms": self.resolution_ms,
            "politeness": self.politeness.model_dump(),
            "max_range": self.max_range,
            "suggested_use": self.suggested_use,
        }


def load_public_sources(path: Path = REGISTRY_FILE) -> dict[str, PublicSource]:
    raw = tomllib.loads(path.read_text())
    out: dict[str, PublicSource] = {}
    for item in raw["source"]:
        entry = PublicSource.model_validate(item)
        if entry.name in out:
            raise ValueError(f"duplicate public source {entry.name!r} in {path.name}")
        out[entry.name] = entry
    return out


PUBLIC_SOURCES = load_public_sources()
