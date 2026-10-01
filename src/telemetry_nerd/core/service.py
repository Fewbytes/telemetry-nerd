"""One operation layer shared by MCP, HTTP, and (later) the sandbox."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.charts.spec import ValidationIssue, auto_spec, validate
from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.core.summary import summarize
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.time import TimeRange, now_ms, parse_duration, parse_time
from telemetry_nerd.sources.base import LimitExceeded, Source, SourceError
from telemetry_nerd.workspace.store import Panel, WorkspaceStore

_NICE_STEPS = [
    parse_duration(s)
    for s in ("15s", "30s", "1m", "2m", "5m", "10m", "15m", "30m", "1h", "2h", "6h", "12h", "1d")
]


def auto_step(rng: TimeRange, resolution_ms: int, target_buckets: int = 600) -> int:
    wanted = max((rng.end_ms - rng.start_ms) / target_buckets, resolution_ms)
    return next((s for s in _NICE_STEPS if s >= wanted), _NICE_STEPS[-1])


MAX_BUCKETS_PER_QUERY = 50_000


class ChartRejected(Exception):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        super().__init__("; ".join(f"[{i.rule}] {i.message}" for i in issues))
        self.issues = issues


@dataclass(frozen=True)
class ShowResult:
    panel: Panel
    issues: list[ValidationIssue]


@dataclass
class TelemetryService:
    sources: dict[str, Source]
    cache: SeriesCache
    datasets: DatasetStore
    workspace: WorkspaceStore
    log: EventLog
    ws: WorkspaceService
    clock: Callable[[], int] = now_ms

    async def query(
        self,
        expr: str,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
        actor: Actor = "claude",
    ) -> dict:
        src = self.sources.get(source)
        if src is None:
            raise SourceError(
                f"unknown source {source!r}",
                hint=f"available sources: {', '.join(sorted(self.sources))}",
            )
        now = self.clock()
        rng = TimeRange(parse_time(start, now), parse_time(end, now))
        step_ms = auto_step(rng, src.resolution_ms) if step == "auto" else parse_duration(step)
        if step_ms <= 0:
            raise SourceError(
                f"step must be positive, got {step!r}",
                hint="use `auto` or a positive duration like 30s, 1m, 5m",
            )
        rng = rng.align(step_ms)
        buckets = (rng.end_ms - rng.start_ms) // step_ms + 1
        if buckets > MAX_BUCKETS_PER_QUERY:
            raise LimitExceeded(
                f"{buckets} buckets exceeds {MAX_BUCKETS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        result = await self.cache.get(
            src.identity, expr, rng, step_ms, lambda r: src.fetch(expr, r, step_ms)
        )
        meta = self.datasets.put(
            source=src.name,
            expr=expr,
            rng=rng,
            step_ms=step_ms,
            resolution_ms=src.resolution_ms,
            result=result,
        )
        summary = summarize(meta, result, now_ms=now, settle_ms=self.cache.settle_ms)
        self.log.append(actor, "dataset.created", meta.id, {"expr": expr})
        return {"dataset": meta.id, "summary": summary}

    def show(self, dataset_id: str, question: str, actor: Actor = "claude") -> ShowResult:
        meta, result = self.datasets.get(dataset_id)
        spec = auto_spec(dataset_id, expr=meta.expr)
        issues = validate(spec, {dataset_id: result.series.num_rows})
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise ChartRejected(errors)
        with self.log.transaction():
            panel = self.workspace.create_panel(question, spec.model_dump(), [dataset_id])
            self.log.append(actor, "panel.created", panel.id, {"question": panel.question})
        return ShowResult(panel, [i for i in issues if i.severity == "warning"])

    def panel_data(self, panel_id: str, width_px: int) -> dict:
        panel = self.workspace.get_panel(panel_id)
        meta, result = self.datasets.get(panel.dataset_ids[0])
        table, effective_step = lod(
            result.buckets, meta.step_ms, TimeRange(meta.start_ms, meta.end_ms), width_px
        )
        labels = {}
        for r in result.series.to_pylist():
            try:
                labels[r["series_id"]] = json.loads(r["labels"])
            except (TypeError, json.JSONDecodeError):
                labels[r["series_id"]] = {}  # labels are self-produced; never block the panel
        series = []
        for (sid,), group in pl.DataFrame(table).group_by("series_id", maintain_order=True):
            cols = {c: group[c].to_list() for c in ("ts_ms", "avg", "min", "max", "count")}
            cols["ts"] = cols.pop("ts_ms")
            series.append({"id": sid, "labels": labels.get(sid, {}), **cols})
        caveats = summarize(meta, result, now_ms=self.clock(), settle_ms=self.cache.settle_ms)[
            "caveats"
        ]
        return {
            "panel": panel.to_dict(),
            "dataset": meta.to_dict(),
            "effective_step_ms": effective_step,
            "series": series,
            "caveats": caveats,
        }
