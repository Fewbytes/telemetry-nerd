"""Create, list, switch and update workspaces; reopening re-attaches recorded sources
(spec "Switch" and "Sources on reopen", D6/D8)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import SourceSpec
from telemetry_nerd.workspace.registry import WorkspaceInfo, WorkspaceRegistry
from telemetry_nerd.workspace.scope import ActiveWorkspace

_log = logging.getLogger(__name__)

OPEN_THREADS_SHOWN = 10

Connect = Callable[..., Awaitable[dict]]


class WorkspaceOps:
    def __init__(
        self,
        registry: WorkspaceRegistry,
        active: ActiveWorkspace,
        log: EventLog,
        sources: SourceRegistry,
        connect: Connect,
        open_threads: Callable[[], list[dict]],
    ) -> None:
        self._registry = registry
        self._active = active
        self._log = log
        self._sources = sources
        self._connect = connect  # TelemetryService.source_connect
        self._open_threads = open_threads  # in the current workspace scope
        self._noted: set[tuple[str, str]] = set()

    async def create(self, title: str, question: str | None, actor: Actor) -> dict:
        info = self._registry.create(title, question)
        return await self.switch(info.id, actor, created=True)

    async def switch(self, wid: str, actor: Actor, created: bool = False) -> dict:
        info = self._registry.get(wid)  # NotFound
        previous = self._active.active
        if wid == previous and not created:
            return await self._result(info, previous)
        if info.archived:
            info = self._registry.update(wid, archived=False)
        with self._active.using(wid):
            self._log.append(
                actor,
                "workspace.opened",
                wid,
                {"title": info.title, "question": info.question, "created": created,
                 "from": previous},
            )  # fmt: skip
        self._registry.mark_opened(wid)
        self._active.set_active(wid)
        result = await self._result(self._registry.get(wid), previous)
        self._notify()
        return {**result, "created": True} if created else result

    def update(
        self,
        wid: str,
        *,
        title: str | None = None,
        question: str | None = None,
        archived: bool | None = None,
        actor: Actor,
    ) -> WorkspaceInfo:
        if archived and wid == self._active.active:
            raise ValueError(
                f"{wid} is the active workspace and cannot be archived: switch to another "
                "one first (workspace_create or workspace_switch)"
            )
        before = self._registry.get(wid)
        info = self._registry.update(wid, title=title, question=question, archived=archived)
        changed = {
            k: v
            for k, v in (("title", info.title), ("question", info.question),
                         ("archived", info.archived))
            if v != getattr(before, k)
        }  # fmt: skip
        if changed:
            with self._active.using(wid):
                self._log.append(actor, "workspace.updated", wid, changed)
            self._notify()
        return info

    def list(self, include_archived: bool = False, limit: int | None = 20) -> dict:
        rows = self._registry.list(include_archived)
        shown = rows if limit is None else rows[:limit]
        return {
            "active": self._active.active,
            "workspaces": [i.to_dict() for i in shown],
            "more": len(rows) - len(shown),
        }

    # sources (D8) -------------------------------------------------------
    def note_source(self, name: str) -> None:
        """Record that the current workspace queried `name`. Sources without a runtime spec
        (settings-owned, e.g. default) are not recorded."""
        wid = self._active()
        if (wid, name) in self._noted:
            return
        spec = self._sources.spec(name)
        if spec is None or name == "default":
            return
        self._registry.note_source(wid, name, json.loads(spec.model_dump_json()))
        self._noted.add((wid, name))

    async def restore_sources(self, wid: str) -> list[dict]:
        out = []
        for name, raw in self._registry.sources(wid).items():
            out.append(await self._restore(name, raw))
        return out

    async def _restore(self, name: str, raw: dict) -> dict:
        try:
            wanted = SourceSpec.model_validate(raw)
        except ValueError as e:
            return {"name": name, "status": "failed", "error": str(e)}
        have = self._sources.spec(name)
        live = name in self._sources
        if live and have == wanted:
            return {"name": name, "status": "connected"}
        if live or (have is not None and have != wanted):
            return {
                "name": name,
                "status": "conflict",
                "hint": f"{name!r} is attached with a different configuration; "
                "source_connect the recorded one under another name",
            }
        try:
            await self._connect(wanted, replace=have is not None, actor="system")
        except Exception as e:  # noqa: BLE001 - a dead source must not block the switch
            _log.info("restoring source %s failed: %r", name, e)
            return {"name": name, "status": "failed", "error": str(e)}
        return {"name": name, "status": "restored"}

    # internals ----------------------------------------------------------
    async def _result(self, info: WorkspaceInfo, previous: str) -> dict:
        with self._active.using(info.id):
            threads = self._open_threads()[:OPEN_THREADS_SHOWN]
            sources = await self.restore_sources(info.id)
        return {
            "workspace": info.to_dict(),
            "previous": previous,
            "counts": info.counts,
            "open_threads": threads,
            "sources": sources,
        }

    def _notify(self) -> None:
        info = self._registry.get(self._active.active)
        self._active.notify({"kind": "workspace", "active": info.to_dict()})
