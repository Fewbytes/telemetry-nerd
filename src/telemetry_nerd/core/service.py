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
    HIST_PX_PER_BAR,
    PX_PER_COLUMN,
    PX_PER_ROW,
    merge_values,
    rebucket_time,
    window_histogram,
)
from telemetry_nerd.analysis.exprkind import (
    QUANTILE_HINT,
    analyze,
    expand,
    histogram_source,
    looks_like_histogram,
    min_samples,
)
from telemetry_nerd.analysis.fraction import fraction_over, wilson
from telemetry_nerd.analysis.marginal import (
    SAMPLE_N_MIN,
    histogram_of,
    pooled_window,
    sample_bins,
    step_values,
)
from telemetry_nerd.analysis.quantile import attach_counts
from telemetry_nerd.analysis.quantiles import column_quantiles
from telemetry_nerd.analysis.reference import reference_window
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.charts.spec import (
    WINDOW_MARKS,
    ChartSpec,
    Layer,
    Marginal,
    Reference,
    ValidationIssue,
    Window,
    auto_spec,
    validate,
)
from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.core.presence import PresenceRegistry
from telemetry_nerd.core.summary import summarize, summarize_distribution
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.distribution import DIST_N_MIN, QUANTILE_CHOICES
from telemetry_nerd.model.time import (
    TimeRange,
    format_duration,
    iso,
    now_ms,
    parse_duration,
    parse_time,
)
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


def _series_labels(series_table) -> dict[str, dict]:
    """series_id -> labels; labels are self-produced, so a bad row never blocks a panel."""
    out: dict[str, dict] = {}
    for r in series_table.to_pylist():
        try:
            out[r["series_id"]] = json.loads(r["labels"])
        except (TypeError, json.JSONDecodeError):
            out[r["series_id"]] = {}
    return out


def _align_within_limit(rng: TimeRange, step_ms: int, noun: str) -> TimeRange:
    """Align the range to the step and refuse more than MAX_BUCKETS_PER_QUERY of them."""
    rng = rng.align(step_ms)
    count = (rng.end_ms - rng.start_ms) // step_ms + 1
    if count > MAX_BUCKETS_PER_QUERY:
        raise LimitExceeded(
            f"{count} {noun} exceeds {MAX_BUCKETS_PER_QUERY} per query",
            hint="use a coarser step or a shorter range",
        )
    return rng


def _edge_text(v: float) -> float | str:
    return ("+Inf" if v > 0 else "-Inf") if math.isinf(v) else v


def _round_sig(v: float) -> float:
    return float(f"{v:.4g}")


def _empty_window(w: dict) -> dict:
    return {"start_ms": w["start_ms"], "end_ms": w["end_ms"], "n": 0.0, "columns": 0,
            "lo": [], "hi": [], "c": []}  # fmt: skip


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
        rng = _align_within_limit(rng, step_ms, "buckets")
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
        summary = self._time_summary(meta, result, now)
        self.log.append(actor, "dataset.created", meta.id, {"expr": expr})
        return {"dataset": meta.id, "summary": summary}

    def _time_summary(self, meta, result, now: int) -> dict:
        summary = summarize(meta, result, now_ms=now, settle_ms=self.cache.settle_ms)
        if meta.representation == "bucket_agg" and looks_like_histogram(meta.expr):
            summary["caveats"].append("histogram_as_lines")
        return summary

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
        rng = _align_within_limit(rng, step_ms, "columns")
        dist = await src.fetch_histogram(selector, tuple(by), rng, step_ms)
        meta = self.datasets.put_distribution(
            source=src.name, rng=rng, step_ms=step_ms, resolution_ms=src.resolution_ms,
            dist=dist, histogram={"selector": selector.strip(), "by": list(by)}, n_min=DIST_N_MIN,
        )  # fmt: skip
        summary = summarize_distribution(meta, dist, now_ms=now, settle_ms=self.cache.settle_ms)
        self.log.append(actor, "dataset.created", meta.id, {"expr": meta.expr})
        return {"dataset": meta.id, "summary": summary}

    def _distribution_panel_data(self, panel: Panel, dataset_id: str, width_px: int) -> dict:
        meta, dist = self.datasets.get_distribution(dataset_id)
        caveats = summarize_distribution(
            meta, dist, now_ms=self.clock(), settle_ms=self.cache.settle_ms
        )["caveats"]
        labels = _series_labels(dist.series)
        layer = panel.spec["layers"][0]
        if layer["mark"] in WINDOW_MARKS:
            return self._histogram_panel_data(panel, meta, dist, labels, caveats, width_px)
        return self._heatmap_panel_data(panel, meta, dist, labels, caveats, width_px)

    @staticmethod
    def _histogram_panel_data(panel, meta, dist, labels, caveats, width_px) -> dict:
        layer = panel.spec["layers"][0]
        raw = pl.from_arrow(dist.rows)
        rows, value_merge = merge_values(raw, dist.scheme, max(4, width_px // HIST_PX_PER_BAR))
        cols = pl.from_arrow(dist.columns)

        def hists(frame):
            return [
                window_histogram(frame, cols, meta.step_ms, w["start_ms"], w["end_ms"])
                for w in layer["windows"]
            ]

        merged = hists(rows)
        # cumulative views (ecdf, quantile, ccdf) must read source buckets, never merged bars
        exact = hists(raw) if value_merge > 1 else None

        def window(k, w, sid):
            out = {"label": w["label"], **(merged[k].get(sid) or _empty_window(w))}
            if exact is not None:
                src = exact[k].get(sid) or _empty_window(w)
                out["source"] = {"lo": src["lo"], "hi": src["hi"], "c": src["c"]}
            return out

        series = [
            {
                "id": sid,
                "labels": lb,
                "windows": [window(k, w, sid) for k, w in enumerate(layer["windows"])],
            }
            for sid, lb in labels.items()
        ]
        n_min = meta.n_min or 0
        if (
            any(0 < w["n"] < n_min for s in series for w in s["windows"])
            and "low_count" not in caveats
        ):
            caveats.append("low_count")
        return {"kind": "histogram", "mark": layer["mark"], "panel": panel.to_dict(),
                "dataset": meta.to_dict(), "effective_step_ms": meta.step_ms,
                "value_merge": value_merge, "series": series, "caveats": caveats}  # fmt: skip

    @staticmethod
    def _heatmap_panel_data(panel, meta, dist, labels, caveats, width_px) -> dict:
        rows = pl.from_arrow(dist.rows)
        cols = pl.from_arrow(dist.columns).with_columns(pl.lit(1, pl.Int64).alias("cover"))
        n_cols = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        factor = max(1, math.ceil(n_cols / max(1, width_px // PX_PER_COLUMN)))
        step = meta.step_ms * factor
        if factor > 1:
            rows, cols = rebucket_time(rows, cols, step)
        # source buckets holding each q: time-summed counts (additive), never value-merged
        bands = column_quantiles(rows, cols, QUANTILE_CHOICES)
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
                "quantiles": bands.get(sid, {}),
            })  # fmt: skip
        return {
            "kind": "heatmap", "mark": panel.spec["layers"][0]["mark"],
            "panel": panel.to_dict(), "dataset": meta.to_dict(),
            "effective_step_ms": step, "value_merge": value_merge, "facet_height_px": facet_h,
            "series": series, "caveats": caveats,
        }  # fmt: skip

    async def distribution_panel(
        self,
        panel_id: str,
        start_ms: int,
        end_ms: int,
        baseline: str = "previous",
        actor: Actor = "user",
    ) -> Panel:
        if end_ms <= start_ms:
            raise ValueError("selection end must be after its start")
        panel = self.workspace.get_panel(panel_id)
        meta = self.datasets.meta(panel.dataset_ids[0])
        if meta.representation != "distribution":
            if not meta.histogram:
                raise SourceError(
                    "no histogram behind this panel",
                    hint="distributions come from histograms: use query_distribution on the _bucket or native histogram metric",
                )
            src = self._source(meta.source)
            step = max(meta.step_ms, 2 * src.resolution_ms)
            out = await self.query_distribution(
                meta.histogram["selector"], meta.histogram["by"], start=str(meta.start_ms),
                end=str(meta.end_ms), step=format_duration(step), source=meta.source, actor=actor,
            )  # fmt: skip
            meta = self.datasets.meta(out["dataset"])
        windows = [Window(start_ms=start_ms, end_ms=end_ms, label="selection")]
        span = end_ms - start_ms
        if baseline == "previous" and start_ms - span >= meta.start_ms - meta.step_ms:
            windows.append(Window(start_ms=start_ms - span, end_ms=start_ms, label="previous"))
        a, b = iso(start_ms)[11:16], iso(end_ms)[11:16]
        question = f"How are values distributed between {a}Z and {b}Z" + (
            ", compared with the preceding window?" if len(windows) > 1 else "?"
        )
        unit = (panel.spec.get("y") or {}).get("unit")
        return self.show(
            meta.id, question, actor=actor, unit=unit, mark="histogram", windows=windows
        ).panel

    def fraction_over(
        self,
        dataset_id: str,
        x: float,
        start: str | None = None,
        end: str | None = None,
        by_series: bool = False,
    ) -> dict:
        """P(X > x) over a window of a distribution dataset: exact at source bucket edges,
        else bounded by the bucket containing x (never interpolated), with a Wilson 95%
        interval for the sampling noise. Series are merged by summing counts (additive)
        unless `by_series`."""
        meta, dist = self.datasets.get_distribution(dataset_id)
        now = self.clock()
        w0 = meta.start_ms - meta.step_ms if start is None else parse_time(start, now)
        w1 = meta.end_ms if end is None else parse_time(end, now)
        if w1 <= w0:
            raise ValueError("window end must be after its start")
        rows = pl.from_arrow(dist.rows)
        cols = pl.from_arrow(dist.columns)
        labels = _series_labels(dist.series)
        if not by_series:
            rows = rows.with_columns(pl.lit("all").alias("series_id"))
            cols = (  # one column per step: counts are additive across series
                cols.group_by("ts_ms")
                .agg(pl.col("n").sum())
                .with_columns(pl.lit("all").alias("series_id"))
            )
            labels = {"all": {}}
        hists = window_histogram(rows, cols, meta.step_ms, w0, w1)
        n_min = meta.n_min or 0
        out = []
        for sid, lb in labels.items():
            h = hists.get(sid)
            r = fraction_over(h["lo"], h["hi"], h["c"], x) if h else None
            if r is None:
                out.append({"labels": lb, "n": 0, "fraction": None, "caveats": ["no_data"]})
                continue
            lo_ci, hi_ci = wilson(r.above, r.n)[0], wilson(r.above + r.inside, r.n)[1]
            value = r.lo if r.exact else (r.lo + r.hi) / 2
            res = {
                "labels": lb,
                "window": [iso(h["start_ms"]), iso(h["end_ms"])],
                "steps": h["columns"],
                "n": _round_sig(r.n),
                "exact": r.exact,
                "fraction": _round_sig(r.lo) if r.exact else [_round_sig(r.lo), _round_sig(r.hi)],
                "count_over": _round_sig(r.above) if r.exact else [_round_sig(r.above), _round_sig(r.above + r.inside)],
                "ci95": [_round_sig(lo_ci), _round_sig(hi_ci)],
                "caveats": ["low_count"] if r.n < n_min else [],
                # ready for finding_create: the interval covers bucket bound and sampling noise
                "evidence": {
                    "kind": "statistic", "dataset": dataset_id, "name": "fraction_over",
                    "value": value, "interval": [lo_ci, hi_ci], "exact": False,
                    "method": "bucket counts + Wilson 95%",
                    "params": {"x": x, "n": r.n, "exact_at_edge": r.exact},
                },
            }  # fmt: skip
            if not r.exact and r.bucket is not None:
                res["inside_bucket"] = [_edge_text(r.bucket[0]), _edge_text(r.bucket[1])]
            out.append(res)
        return {"dataset": dataset_id, "x": x, "series": out}

    async def ensure_reference(self, panel_id: str, mode: str, actor: Actor) -> Reference:
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        if any(layer.mark != "line+envelope" for layer in spec.layers):
            raise ValueError(
                f"marginals and indexed views apply to time-series panels; {p.id} is not one"
            )
        if (ref := spec.references.get(mode)) is not None:
            return ref
        meta = self.datasets.meta(p.dataset_ids[0])
        rw = reference_window(meta.start_ms, meta.end_ms, meta.step_ms, mode)
        common = {"source": meta.source, "actor": actor}
        series = (
            await self.query(
                meta.expr,
                start=str(rw.start_ms),
                end=str(rw.end_ms),
                step=format_duration(meta.step_ms),
                **common,
            )
        )["dataset"]
        dist = dist_cur = None
        if meta.histogram:
            h, src = meta.histogram, self._source(meta.source)
            dstep = format_duration(max(meta.step_ms, 2 * src.resolution_ms))
            dist_cur = (
                await self.query_distribution(
                    h["selector"],
                    h["by"],
                    start=str(meta.start_ms),
                    end=str(meta.end_ms),
                    step=dstep,
                    **common,
                )
            )["dataset"]
            dist = (
                await self.query_distribution(
                    h["selector"],
                    h["by"],
                    start=str(rw.start_ms),
                    end=str(rw.end_ms),
                    step=dstep,
                    **common,
                )
            )["dataset"]
        return Reference(
            mode=mode,
            label=rw.label,
            start_ms=rw.start_ms,
            end_ms=rw.end_ms,  # type: ignore[arg-type]
            shift_ms=rw.shift_ms,
            series=series,
            dist=dist,
            dist_current=dist_cur,
        )

    async def set_marginal(
        self, panel_id: str, reference: str | None, actor: Actor, reason: str | None = None
    ) -> dict:
        if reference is None:
            self.ws.set_marginal(panel_id, None, None, actor)
            return {"panel": panel_id, "marginal": None}
        ref = await self.ensure_reference(panel_id, reference, actor)
        p = self.ws.set_marginal(
            panel_id, Marginal(reference=ref.mode, author=actor, reason=reason), ref, actor
        )  # type: ignore[arg-type]
        m = self._marginal(ChartSpec.model_validate(p.spec), self.datasets.meta(p.dataset_ids[0]))
        assert m is not None
        now, prev = m["windows"]
        return {
            "panel": p.id,
            "basis": m["basis"],
            "what": m["what"],
            "reference": ref.label,
            "n": {"now": now["n"], "reference": prev["n"]},
            "datasets": [ref.series, ref.dist],
        }

    def _marginal(self, spec: ChartSpec, meta) -> dict | None:
        m = spec.marginal
        ref = spec.references.get(m.reference) if m else None
        if m is None or ref is None:
            return None
        head = {
            "reference": {
                "mode": ref.mode,
                "label": ref.label,
                "start_ms": ref.start_ms,
                "end_ms": ref.end_ms,
            },
            "author": m.author,
            "reason": m.reason,
        }
        if ref.dist and ref.dist_current:
            wins = []
            for did, label in ((ref.dist_current, "now"), (ref.dist, ref.label)):
                dm, dist = self.datasets.get_distribution(did)
                w = pooled_window(pl.from_arrow(dist.rows), pl.from_arrow(dist.columns), dm.step_ms,
                                  dm.start_ms - dm.step_ms, dm.end_ms)  # fmt: skip
                wins.append(
                    {
                        "label": label,
                        "start_ms": dm.start_ms,
                        "end_ms": dm.end_ms,
                        "n": 0.0,
                        "columns": 0,
                        "lo": [],
                        "hi": [],
                        "c": [],
                        **(w or {}),
                    }
                )
            k = max(w.get("series", 1) for w in wins)
            what = f"observations (requests) of {meta.histogram['selector']}" + (
                f", {k} series summed" if k > 1 else ""
            )
            return {
                "basis": "distribution",
                "what": what,
                "n_min": DIST_N_MIN,
                "windows": wins,
                "excluded": [0, 0],
                **head,
            }
        _, cur = self.datasets.get(meta.id)
        rmeta, rres = self.datasets.get(ref.series)
        cv, cx, k = step_values(cur.buckets, meta.representation, meta.n_min)
        rv, rx, _ = step_values(rres.buckets, rmeta.representation, rmeta.n_min)
        edges = sample_bins(cv, rv)
        lo, hi = [a for a, _ in edges], [b for _, b in edges]
        step = format_duration(meta.step_ms)
        kind = (
            f"p{meta.quantile * 100:g} values per {step} step (n ≥ {meta.n_min} only): "
            "a distribution of percentile values, not of requests"
            if meta.representation == "quantile"
            else f"per-step values ({step} means of scrape samples): scrape samples, not requests"
        )
        what = kind + (f"; {k} series pooled" if k > 1 else "")
        wins = [{"label": label, "start_ms": s, "end_ms": e, "n": float(len(v)), "columns": len(v),
                 "lo": lo, "hi": hi, "c": histogram_of(v, edges)}
                for label, s, e, v in (("now", meta.start_ms, meta.end_ms, cv),
                                       (ref.label, ref.start_ms, ref.end_ms, rv))]  # fmt: skip
        return {
            "basis": "samples",
            "what": what,
            "n_min": SAMPLE_N_MIN,
            "windows": wins,
            "excluded": [cx, rx],
            **head,
        }

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

    async def learn(self, source: str = "default", actor: Actor = "system") -> dict:
        """Discover a source and (re-)learn its catalog from the result."""
        discovery = await self._source(source).discover()
        return self.ws.catalog_learn(source, discovery, actor)

    def show(
        self,
        dataset_id: str,
        question: str,
        actor: Actor = "claude",
        unit: str | None = None,
        mark: str = "auto",
        windows: list[Window] | None = None,
        quantiles: list[float] | None = None,
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
            lookup=lambda metric: self.ws.catalog_facts(meta.source, metric),
        )
        if mark != "auto":
            for w in windows or []:
                if not meta.start_ms - meta.step_ms <= w.start_ms < w.end_ms <= meta.end_ms:
                    raise ValueError(
                        f"window {iso(w.start_ms)}..{iso(w.end_ms)} is outside the dataset range"
                    )
            layer = Layer(mark=mark, data=dataset_id, windows=windows or [])  # type: ignore[arg-type]
            if quantiles is not None:
                layer.quantiles = [float(q) for q in quantiles]
            spec.layers = [layer]
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
        labels = _series_labels(result.series)
        series = []
        for (sid,), group in pl.DataFrame(table).group_by("series_id", maintain_order=True):
            cols = {c: group[c].to_list() for c in ("ts_ms", "avg", "min", "max", "count")}
            cols["ts"] = cols.pop("ts_ms")
            series.append({"id": sid, "labels": labels.get(sid, {}), **cols})
        caveats = self._time_summary(meta, result, self.clock())["caveats"]
        return {
            "kind": "time",
            "marginal": self._marginal(ChartSpec.model_validate(panel.spec), meta),
            "panel": panel.to_dict(),
            "dataset": meta.to_dict(),
            "effective_step_ms": effective_step,
            "series": series,
            "caveats": caveats,
        }
