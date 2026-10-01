"""Live sources by name. Runtime-added specs persist in SQLite and reload at startup."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping

from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.base import Source, SourceError
from telemetry_nerd.sources.spec import SourceSpec

Factory = Callable[[SourceSpec], Source]


class SourceRegistry(Mapping[str, Source]):
    def __init__(
        self, db: sqlite3.Connection, factory: Factory, clock: Callable[[], int] = now_ms
    ) -> None:
        self._db = db
        self._factory = factory
        self._clock = clock
        self._live: dict[str, Source] = {}
        self._specs: dict[str, SourceSpec] = {}
        self._attached: set[str] = set()  # settings-owned, never persisted
        self._broken: dict[str, str] = {}

    # Mapping: only live sources are queryable
    def __getitem__(self, name: str) -> Source:
        return self._live[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._live)

    def __len__(self) -> int:
        return len(self._live)

    def load(self) -> None:
        rows = self._db.execute("SELECT name, spec FROM sources ORDER BY created_at_ms, name")
        for name, raw in rows.fetchall():
            spec = SourceSpec.model_validate_json(raw)
            self._specs[name] = spec
            try:
                self._live[name] = self._factory(spec)
            except SourceError as e:
                # e.g. the secret's env var is not set in this daemon: keep the spec, report it
                self._broken[name] = str(e)

    def build(self, spec: SourceSpec) -> Source:
        return self._factory(spec)

    def spec(self, name: str) -> SourceSpec | None:
        return self._specs.get(name)

    def add(self, spec: SourceSpec, source: Source, *, replace: bool = False) -> Source | None:
        if spec.name in self._specs and not replace:
            raise SourceError(
                f"source {spec.name!r} already exists",
                hint="pass replace=true to reconfigure it, or choose another name",
            )
        self._db.execute(
            "INSERT INTO sources (name, spec, created_at_ms) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET spec = excluded.spec",
            (spec.name, spec.model_dump_json(), self._clock()),
        )
        old = self._live.get(spec.name)
        self._specs[spec.name] = spec
        self._live[spec.name] = source
        self._broken.pop(spec.name, None)
        return old

    def attach(self, name: str, source: Source, spec: SourceSpec | None = None) -> None:
        self._live[name] = source
        self._attached.add(name)
        if spec is not None:
            self._specs[name] = spec

    def remove(self, name: str) -> Source | None:
        if name not in self._specs and name not in self._live:
            raise SourceError(f"unknown source {name!r}", hint="see source_list for names")
        self._db.execute("DELETE FROM sources WHERE name = ?", (name,))
        self._specs.pop(name, None)
        self._broken.pop(name, None)
        self._attached.discard(name)
        return self._live.pop(name, None)

    def describe(self) -> list[dict]:
        out = []
        for name in sorted(set(self._specs) | set(self._live)):
            spec = self._specs.get(name)
            entry = spec.public() if spec else {"name": name}
            entry["managed_by"] = "settings" if name in self._attached else "runtime"
            entry["live"] = name in self._live
            entry["broken"] = self._broken.get(name)
            out.append(entry)
        return out
