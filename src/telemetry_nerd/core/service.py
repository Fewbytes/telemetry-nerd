"""One operation layer shared by MCP, HTTP, and (later) the sandbox."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import polars as pl

from telemetry_nerd.analysis.distlod import (
    FACET_HEIGHT_MULTI,
    FACET_HEIGHT_SINGLE,
    PX_PER_COLUMN,
    PX_PER_ROW,
    merge_values,
    rebucket_time,
)
from telemetry_nerd.analysis.exprkind import (
    QUANTILE_HINT,
    analyze,
    expand,
    histogram_source,
    min_samples,
)
from telemetry_nerd.analysis.quantile import attach_counts
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.charts.spec import ValidationIssue, auto_spec, validate
from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.core.presence import PresenceRegistry
from telemetry_nerd.core.summary import summarize, summarize_distribution
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.distribution import DIST_N_MIN
from telemetry_nerd.model.time import TimeRange, format_duration, now_ms, parse_duration, parse_time
from telemetry_nerd.sources.base import LimitExceeded, Source, SourceError
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import RESERVED_NAMES, SourceSpec
from telemetry_nerd.workspace.store import Panel, WorkspaceStore

_NICE_STEPS = [
    parse_duration(s)
    for s in ("15s", "30s", "1m", "2m", "5m", "10m", "15m", "30m", "1h", "2h", "6h", "12h", "1d")
]


def auto_step(rng: TimeRange, resolution_ms: int, target_buckets: int = 600) -> int:
    wanted = max((rng.end_ms - rng.start_ms) / target_buckets, resolution_ms)
    return next((s for s in _NICE_STEPS if s >= wanted), _NICE_STEPS[-1])


MAX_BUCKETS_PER_QUERY = 50_000
DIST_TARGET_COLUMNS = 300


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
    sources: SourceRegistry
    cache: SeriesCache
    datasets: DatasetStore
    workspace: WorkspaceStore
    log: EventLog
    ws: WorkspaceService
    clock: Callable[[], int] = now_ms
    presence: PresenceRegistry = field(default_factory=PresenceRegistry)

    def _source(self, name: str) -> Source:
        src = self.sources.get(name)
        if src is None:
            raise SourceError(
                f"unknown source {name!r}",
                hint=(
                    f"available sources: {', '.join(sorted(self.sources)) or 'none'}; "
                    "connect one with source_connect"
                ),
            )
        return src

    async def query(
        self,
        expr: str,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
        actor: Actor = "claude",
    ) -> dict:
        src = self._source(source)
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
        expr = expand(expr, step_ms, src.resolution_ms)
        info = analyze(expr)
        if info.problem:
            raise SourceError(info.problem, hint=QUANTILE_HINT)
        representation, q, n_min = "bucket_agg", None, None
        histogram = None
        if info.quantile is None:
            result = await self.cache.get(
                src.identity, expr, rng, step_ms, lambda r: src.fetch(expr, r, step_ms)
            )
        else:
            qx = info.quantile
            representation, q = "quantile", qx.q
            if hs := histogram_source(expr):
                histogram = {"selector": hs.selector, "by": list(hs.by)}
            result = await self.cache.get(
                src.identity,
                f"values|{expr}",
                rng,
                step_ms,
                lambda r: src.fetch_values(expr, r, step_ms),
            )
            if qx.count_expr is not None:
                count_expr = qx.count_expr
                counts = await self.cache.get(
                    src.identity,
                    f"values|{count_expr}",
                    rng,
                    step_ms,
                    lambda r: src.fetch_values(count_expr, r, step_ms),
                )
                result = attach_counts(result, counts)
                n_min = min_samples(q) if q is not None else None
        meta = self.datasets.put(
            source=src.name,
            expr=expr,
            rng=rng,
            step_ms=step_ms,
            resolution_ms=src.resolution_ms,
            result=result,
            representation=representation,
            quantile=q,
            n_min=n_min,
            histogram=histogram,
        )
        summary = summarize(meta, result, now_ms=now, settle_ms=self.cache.settle_ms)
        self.log.append(actor, "dataset.created", meta.id, {"expr": expr})
        return {"dataset": meta.id, "summary": summary}

    async def query_distribution(
        self,
        selector: str,
        by: Sequence[str] = (),
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
        actor: Actor = "claude",
    ) -> dict:
        src = self._source(source)
        now = self.clock()
        rng = TimeRange(parse_time(start, now), parse_time(end, now))
        floor = 2 * src.resolution_ms  # increase() needs two samples per window
        step_ms = (
            auto_step(rng, floor, DIST_TARGET_COLUMNS) if step == "auto" else parse_duration(step)
        )
        if step_ms < floor:
            raise SourceError(
                f"step {format_duration(step_ms)} is shorter than two scrape intervals "
                f"({format_duration(floor)})",
                hint=f"counts come from increase() per step; use step >= {format_duration(floor)} or auto",
            )
        rng = rng.align(step_ms)
        columns = (rng.end_ms - rng.start_ms) // step_ms + 1
        if columns > MAX_BUCKETS_PER_QUERY:
            raise LimitExceeded(
                f"{columns} columns exceeds {MAX_BUCKETS_PER_QUERY} per query",
                hint="use a coarser step or a shorter range",
            )
        dist = await src.fetch_histogram(selector, tuple(by), rng, step_ms)
        meta = self.datasets.put_distribution(
            source=src.name, rng=rng, step_ms=step_ms, resolution_ms=src.resolution_ms,
            dist=dist, histogram={"selector": selector.strip(), "by": list(by)}, n_min=DIST_N_MIN,
        )  # fmt: skip
        summary = summarize_distribution(meta, dist, now_ms=now, settle_ms=self.cache.settle_ms)
        self.log.append(actor, "dataset.created", meta.id, {"expr": meta.expr})
        return {"dataset": meta.id, "summary": summary}

    @staticmethod
    def _labels(series_table) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in series_table.to_pylist():
            try:
                out[r["series_id"]] = json.loads(r["labels"])
            except (TypeError, json.JSONDecodeError):
                out[r["series_id"]] = {}  # labels are self-produced; never block the panel
        return out

    def _distribution_panel_data(self, panel: Panel, dataset_id: str, width_px: int) -> dict:
        meta, dist = self.datasets.get_distribution(dataset_id)
        caveats = summarize_distribution(
            meta, dist, now_ms=self.clock(), settle_ms=self.cache.settle_ms
        )["caveats"]
        rows = pl.from_arrow(dist.rows)
        cols = pl.from_arrow(dist.columns).with_columns(pl.lit(1, pl.Int64).alias("cover"))
        labels = self._labels(dist.series)
        n_cols = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        factor = max(1, math.ceil(n_cols / max(1, width_px // PX_PER_COLUMN)))
        step = meta.step_ms * factor
        if factor > 1:
            rows, cols = rebucket_time(rows, cols, step)
        facet_h = FACET_HEIGHT_SINGLE if len(labels) <= 1 else FACET_HEIGHT_MULTI
        rows, value_merge = merge_values(rows, dist.scheme, max(4, facet_h // PX_PER_ROW))
        series = []
        for sid, lb in labels.items():
            c = cols.filter(pl.col("series_id") == sid)
            r = rows.filter(pl.col("series_id") == sid)
            series.append({
                "id": sid, "labels": lb,
                "ts": c["ts_ms"].to_list(), "n": c["n"].to_list(), "cover": c["cover"].to_list(),
                "cells": {"ts": r["ts_ms"].to_list(), "lo": r["bucket_lo"].to_list(),
                          "hi": r["bucket_hi"].to_list(), "c": r["count"].to_list()},
            })  # fmt: skip
        return {
            "kind": "heatmap", "panel": panel.to_dict(), "dataset": meta.to_dict(),
            "effective_step_ms": step, "value_merge": value_merge, "facet_height_px": facet_h,
            "series": series, "caveats": caveats,
        }  # fmt: skip

    @staticmethod
    def _refuse_reserved(name: str) -> None:
        if name in RESERVED_NAMES:
            raise SourceError(
                f"source name {name!r} is reserved for the daemon's configured source",
                hint="choose another name, e.g. the host or Grafana datasource name",
            )

    @staticmethod
    async def _close(source: object) -> None:
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            await aclose()

    async def source_connect(
        self, spec: SourceSpec, *, replace: bool = False, actor: Actor = "claude"
    ) -> dict:
        self._refuse_reserved(spec.name)
        if self.sources.spec(spec.name) is not None and not replace:
            raise SourceError(
                f"source {spec.name!r} already exists",
                hint="pass replace=true to reconfigure it, or choose another name",
            )
        source = self.sources.build(spec)  # raises MissingSecret before any network call
        try:
            status = await source.probe()
        except SourceError:
            await self._close(source)
            raise
        old = self.sources.add(spec, source, replace=replace)
        if old is not None:
            await self._close(old)
        public = spec.public()
        self.log.append(actor, "source.connected", spec.name, {"source": public})
        return {"source": public, "status": status}

    def source_list(self) -> list[dict]:
        return self.sources.describe()

    async def source_status(self, name: str) -> dict:
        source = self.sources.get(name)
        if source is None:
            raise SourceError(f"unknown source {name!r}", hint="see source_list for names")
        try:
            return await source.probe()
        except SourceError as e:
            return {"reachable": False, "error": str(e), "hint": e.hint}

    async def source_disconnect(self, name: str, actor: Actor = "claude") -> None:
        self._refuse_reserved(name)
        old = self.sources.remove(name)
        if old is not None:
            await self._close(old)
        self.log.append(actor, "source.disconnected", name, {})

    def show(
        self, dataset_id: str, question: str, actor: Actor = "claude", unit: str | None = None
    ) -> ShowResult:
        meta = self.datasets.meta(dataset_id)
        # An agent-provided unit (Claude learned it from the source, the emitting
        # tool, etc.) wins over suffix inference and records who vouched for it.
        spec = auto_spec(
            dataset_id,
            expr=meta.expr,
            unit=unit,
            unit_provenance=f"provided by {actor}" if unit else None,
            representation=meta.representation,
        )
        issues = validate(
            spec,
            {dataset_id: self.datasets.series_count(dataset_id)},
            {dataset_id: meta.representation},
        )
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise ChartRejected(errors)
        with self.log.transaction():
            panel = self.workspace.create_panel(question, spec.model_dump(), [dataset_id])
            self.log.append(actor, "panel.created", panel.id, {"question": panel.question})
        return ShowResult(panel, [i for i in issues if i.severity == "warning"])

    def panel_data(self, panel_id: str, width_px: int) -> dict:
        panel = self.workspace.get_panel(panel_id)
        dataset_id = panel.dataset_ids[0]
        if self.datasets.meta(dataset_id).representation == "distribution":
            return self._distribution_panel_data(panel, dataset_id, width_px)
        meta, result = self.datasets.get(dataset_id)
        if meta.representation == "quantile":
            # never re-aggregate percentiles over time: serve at their own step
            table, effective_step = result.buckets, meta.step_ms
        else:
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
            "kind": "time",
            "panel": panel.to_dict(),
            "dataset": meta.to_dict(),
            "effective_step_ms": effective_step,
            "series": series,
            "caveats": caveats,
        }
