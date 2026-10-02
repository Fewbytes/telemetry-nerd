"""One operation layer shared by MCP, HTTP, and (later) the sandbox."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import polars as pl

from telemetry_nerd.analysis.distlod import (
    FACET_HEIGHT_SINGLE,
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
from telemetry_nerd.analysis.filters import FilterSpec
from telemetry_nerd.analysis.fraction import fraction_over, wilson
from telemetry_nerd.analysis.quantile import attach_counts
from telemetry_nerd.analysis.reference import reference_window
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.catalog.profiles import ProfileStore
from telemetry_nerd.charts.dataview import SignalViews, offered_views
from telemetry_nerd.charts.spec import (
    SPECTRAL_MARKS,
    WINDOW_MARKS,
    ChartSpec,
    Layer,
    Marginal,
    Reference,
    ValidationIssue,
    Window,
    YContext,
    YLimit,
    YProfile,
    auto_spec,
    validate,
)
from telemetry_nerd.charts.ycontext import (
    counter_rate_metric,
    limit_expr,
    natural_range,
    selector_parts,
)
from telemetry_nerd.charts.yview import value_stats
from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.core.panel_payloads import (
    ghost_payload,
    heatmap_panel_data,
    histogram_panel_data,
    index_payload,
    limit_payload,
    marginal_payload,
    normal_payload,
    series_labels,
    series_payload,
    signal_payload,
)
from telemetry_nerd.core.presence import PresenceRegistry
from telemetry_nerd.core.profiles import ProfileService
from telemetry_nerd.core.signal_ops import SignalOps
from telemetry_nerd.core.summary import summarize, summarize_distribution
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.distribution import DIST_N_MIN
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
PROFILE_WAIT_S = 8.0  # how long `show` waits for a first-view operating profile
DIST_TARGET_COLUMNS = 300


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


_NICE_SEGMENTS = [m * 60_000 for m in (5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880)]


def _auto_segment(span_ms: int, step_ms: int) -> int:
    """Nice duration nearest span/16, clamped to [16 steps, span/4]."""
    want = span_ms / 16
    seg = min(_NICE_SEGMENTS, key=lambda s: abs(s - want))
    return int(min(max(seg, 16 * step_ms), span_ms // 4))


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
    signal: SignalOps = field(init=False)
    profiles: ProfileService = field(init=False)
    #: compute a T1 operating profile in the background when a time-series panel is shown
    auto_profile: bool = False

    def __post_init__(self) -> None:
        self.signal = SignalOps(self.datasets, self.ws.catalog_facts)
        self.profiles = ProfileService(
            self.sources, self.cache, ProfileStore(self.workspace.connection), self.ws, self.clock
        )

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
        labels = series_labels(dist.series)
        layer = panel.spec["layers"][0]
        if layer["mark"] in WINDOW_MARKS:
            return histogram_panel_data(panel, meta, dist, labels, caveats, width_px)
        return heatmap_panel_data(panel, meta, dist, labels, caveats, width_px)

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
        labels = series_labels(dist.series)
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

    async def y_context(self, panel_id: str, actor: Actor = "system") -> YContext | None:
        """Work out what the catalog says about a time panel's y axis and record it (bead 2as.10).

        Natural bounds come from the metric's catalog `bounds` claim; the physical limit is the
        `bounded_by` metric fetched under the same label matchers. Only plain selectors (and
        rate/increase of one counter) qualify: bounds of a mixed expression belong to no single
        metric. A source failure degrades to a note: the panel still renders."""
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        if any(layer.mark != "line+envelope" for layer in spec.layers) or spec.signal:
            return None
        meta = self.datasets.meta(p.dataset_ids[0])
        ctx = YContext()
        parts = selector_parts(meta.expr)
        metric = parts[0] if parts else counter_rate_metric(meta.expr)
        if metric is None:
            ctx.notes.append("natural_bounds_unknown: the expression is not a single metric")
        elif parts is not None:
            if found := self.ws.catalog_bounds(meta.source, metric):
                ctx.bounds, ctx.bounds_origin = found
                ctx.natural_lo, ctx.natural_hi = natural_range(found[0])
        elif self.ws.catalog_facts(meta.source, metric).type == "counter":
            # a rate of a counter is never negative, whatever the counter's own bounds say
            ctx.natural_lo, ctx.bounds, ctx.bounds_origin = 0.0, "≥0", "counter rate"
        if parts is not None:
            if targets := self.ws.catalog_bounded_by(meta.source, parts[0]):
                ctx.limit = await self._fetch_limit(meta, parts[1], targets[0], ctx.notes)
        elif metric is not None and self.ws.catalog_bounded_by(meta.source, metric):
            ctx.notes.append(
                "limit_unavailable: the physical limit bounds the metric itself, not its rate"
            )
        ctx.profile = await self._fetch_profile(meta, ctx.notes)
        self.ws.set_y_context(p.id, ctx, actor)
        return ctx

    async def _fetch_profile(self, meta: DatasetMeta, notes: list[str]) -> YProfile | None:
        """The operating range of what the panel shows, if it is (or soon will be) known.

        The first view computes the profile (a long-window query): wait briefly, then let the
        computation finish in the background and tell the panel to look again."""
        try:
            prof = await asyncio.wait_for(
                self.profiles.ensure(meta.source, meta.expr), PROFILE_WAIT_S
            )
        except TimeoutError:
            notes.append("profile_pending: the operating profile is still being computed")
            return None
        except (SourceError, ValueError) as e:
            notes.append(f"profile_unavailable: {e}")
            return None
        r = prof.pooled
        lo, hi = (r.envelope_lo, r.envelope_hi) if prof.extremes else (r.p005, r.p995)
        if lo is None or hi is None or not lo < hi:
            notes.append("profile_unavailable: the profile has no usable range")
            return None
        return YProfile(lo=lo, hi=hi, label=f"normal range ({format_duration(prof.window_ms)})")

    async def _fetch_limit(
        self, meta: DatasetMeta, matchers: str, target: str, notes: list[str]
    ) -> YLimit | None:
        expr = limit_expr(matchers, target)
        try:
            ds = (
                await self.query(
                    expr,
                    start=str(meta.start_ms),
                    end=str(meta.end_ms),
                    step=format_duration(meta.step_ms),
                    source=meta.source,
                    actor="system",
                )
            )["dataset"]
            m, result = self.datasets.get(ds)
            hi = value_stats(result.buckets, m.representation, m.n_min).hi
        except (SourceError, ValueError) as e:
            notes.append(f"limit_unavailable: {target} could not be fetched ({e})")
            return None
        if hi is None:
            notes.append(f"limit_unavailable: {target} has no data under these labels")
            return None
        return YLimit(metric=target, dataset=ds, hi=hi)

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
        m = marginal_payload(
            self.datasets, ChartSpec.model_validate(p.spec), self.datasets.meta(p.dataset_ids[0])
        )
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

    def spectrum(
        self,
        dataset_id: str,
        top: int = 3,
        min_period: str | None = None,
        max_period: str | None = None,
    ) -> dict:
        """Dominant periods with intervals and false-alarm probabilities (bead 4ok.7)."""
        return self.signal.spectrum_summary(
            dataset_id,
            top,
            parse_duration(min_period) if min_period else None,
            parse_duration(max_period) if max_period else None,
        )

    def filter(
        self,
        dataset_id: str,
        kind: str,
        period: str,
        reason: str,
        period_hi: str | None = None,
        actor: Actor = "claude",
    ) -> dict:
        """Derive a low-/high-/band-pass filtered dataset; cutoff is a period (bead 4ok.9)."""
        if kind not in ("lowpass", "highpass", "bandpass"):
            raise ValueError("kind must be lowpass, highpass or bandpass")
        spec = FilterSpec(
            kind,  # type: ignore[arg-type]
            parse_duration(period),
            parse_duration(period_hi) if period_hi else None,
        )
        meta, result, extra = self.signal.filter(dataset_id, spec, reason)
        new = self.datasets.put(
            source=meta.source,
            expr=meta.expr,
            rng=TimeRange(meta.start_ms, meta.end_ms),
            step_ms=meta.step_ms,
            resolution_ms=meta.resolution_ms,
            result=result,
            representation=meta.representation,
            quantile=meta.quantile,
            n_min=meta.n_min,
            histogram=meta.histogram,
            derived=extra["derived"],
        )
        self.log.append(
            actor,
            "dataset.created",
            new.id,
            {
                "expr": meta.expr,
                "derived": {k: extra["derived"][k] for k in ("op", "from", "label")},
            },
        )
        return {"dataset": new.id, "summary": extra["summary"]}

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
        view: str | None = None,
        segment: str | None = None,
        overlap: float | None = None,
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
        panel_datasets = [dataset_id]
        if mark in SPECTRAL_MARKS:
            self.signal.check(dataset_id, mark)
            layer = Layer(mark=mark, data=dataset_id)  # type: ignore[arg-type]
            if mark == "spectrogram":
                span = meta.end_ms - meta.start_ms + meta.step_ms
                seg = parse_duration(segment) if segment else _auto_segment(span, meta.step_ms)
                lo, hi = 16 * meta.step_ms, span // 4
                if not lo <= seg <= hi:
                    raise ValueError(
                        f"segment {format_duration(seg)} must lie between {format_duration(lo)} "
                        f"(16 steps) and {format_duration(hi)} (a quarter of the range)"
                    )
                layer.segment_ms, layer.overlap = seg, 0.5 if overlap is None else overlap
            spec.layers = [layer]
        elif meta.derived and mark == "auto":
            d = meta.derived
            views = offered_views(d["op"])
            if view is not None and view not in views:
                raise ValueError(
                    f"view {view!r} is not offered for a {d['op']} ({', '.join(views)})"
                )
            spec.layers = [
                Layer(mark="line+envelope", data=dataset_id),
                Layer(mark="line+envelope", data=d["from"], role="context"),
            ]
            spec.signal = SignalViews(
                filter=d["label"], kind=d["op"], reason=d["reason"], offered=views,
                default=view or views[0],
            )  # type: ignore[arg-type]  # fmt: skip
            panel_datasets = [dataset_id, d["from"]]
        elif mark != "auto":
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
            {d: self.datasets.series_count(d) for d in panel_datasets},
            {d: self.datasets.meta(d).representation for d in panel_datasets},
        )
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise ChartRejected(errors)
        with self.log.transaction():
            panel = self.workspace.create_panel(question, spec.model_dump(), panel_datasets)
            self.log.append(actor, "panel.created", panel.id, {"question": panel.question})
        if self.auto_profile and spec.layers[0].mark == "line+envelope" and not meta.derived:
            self.profiles.request(meta.source, meta.expr)  # lazy T1 profile on first view
        return ShowResult(panel, [i for i in issues if i.severity == "warning"])

    async def operating_profile(
        self, expr: str, source: str = "default", refresh: bool = False
    ) -> dict:
        """T1 operating profile of what `expr` shows, computed now if missing or a day old."""
        p = await self.profiles.ensure(source, expr, force=refresh)
        return p.summary()

    def _overlays(self, panel, meta, series, labels, step_ms: int, width_px: int) -> dict:
        """Normal band, limit line and last-week ghost: what each is, whether it is on, and its
        data when on. Disabled chips carry their reason (bead 2as.11)."""
        spec = ChartSpec.model_validate(panel.spec)
        if any(layer.mark != "line+envelope" for layer in spec.layers) or spec.signal:
            return {}
        ov = spec.overlays
        profile = self.profiles.cached(meta.source, meta.expr)
        window = format_duration(profile.window_ms) if profile else ""
        normal = normal_payload(profile, series, step_ms, window)
        limit = limit_payload(self.datasets, spec.y.context, meta, labels, width_px)
        ghost = ghost_payload(self.datasets, spec, meta, width_px) if ov.ghost else {
            "available": True, "loaded": "week" in spec.references, "label": "last week",
        }  # fmt: skip
        out = {"flags": ov.model_dump(), "normal": normal, "limit": limit, "ghost": ghost}
        for name in ("normal", "limit"):  # data only when on: availability always
            if not getattr(ov, name):
                out[name] = {k: v for k, v in out[name].items() if k not in ("series", "unmatched")}
        return out

    async def set_overlays(
        self,
        panel_id: str,
        actor: Actor,
        normal: bool | None = None,
        limit: bool | None = None,
        ghost: bool | None = None,
    ) -> dict:
        """Toggle reference layers on a time panel; turning the ghost on fetches last week."""
        ref = None
        if ghost:
            ref = await self.ensure_reference(panel_id, "week", actor)
        p = self.ws.set_overlays(
            panel_id, actor, normal=normal, limit=limit, ghost=ghost, reference=ref
        )
        return {"panel": p.id, "overlays": p.spec["overlays"]}

    def panel_data(self, panel_id: str, width_px: int) -> dict:
        panel = self.workspace.get_panel(panel_id)
        dataset_id = panel.dataset_ids[0]
        layer0 = panel.spec["layers"][0]
        if layer0["mark"] == "spectrum":
            out = self.signal.spectrum_panel(
                dataset_id, layer0.get("min_period_ms"), layer0.get("max_period_ms"), width_px
            )
            return {
                "panel": panel.to_dict(),
                "dataset": self.datasets.meta(dataset_id).to_dict(),
                **out,
            }
        if layer0["mark"] == "spectrogram":
            out = self.signal.spectrogram_panel(
                dataset_id, layer0["segment_ms"], layer0["overlap"], FACET_HEIGHT_SINGLE
            )
            return {
                "panel": panel.to_dict(),
                "dataset": self.datasets.meta(dataset_id).to_dict(),
                **out,
            }
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
        labels = series_labels(result.series)
        series = series_payload(table, labels)
        caveats = self._time_summary(meta, result, self.clock())["caveats"]
        extra = signal_payload(self.datasets, panel, meta, width_px, labels)
        if extra:
            caveats.append("filtered")
        return {
            **extra,
            "kind": "time",
            "marginal": marginal_payload(self.datasets, ChartSpec.model_validate(panel.spec), meta),
            "index": index_payload(
                self.datasets, ChartSpec.model_validate(panel.spec), meta, result, width_px
            ),
            "overlays": self._overlays(panel, meta, series, labels, effective_step, width_px),
            "panel": panel.to_dict(),
            "dataset": meta.to_dict(),
            "effective_step_ms": effective_step,
            "series": series,
            "caveats": caveats,
        }
