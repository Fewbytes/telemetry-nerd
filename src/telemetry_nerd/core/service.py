"""One operation layer shared by MCP, HTTP, and (later) the tier-2 `tn` library."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.compute as pc

from telemetry_nerd.analysis.distlod import (
    FACET_HEIGHT_SINGLE,
    window_histogram,
)
from telemetry_nerd.analysis.exprkind import (
    QUANTILE_HINT,
    RATE_INTERVAL,
    analyze,
    expand,
    has_division,
    histogram_source,
    looks_like_histogram,
    min_samples,
)
from telemetry_nerd.analysis.filters import FilterSpec
from telemetry_nerd.analysis.fraction import fraction_over, wilson
from telemetry_nerd.analysis.outcome import OUTCOME_LABELS, add_matcher, candidate_labels, classify
from telemetry_nerd.analysis.profile_reference import describe, hourly_means, matched_hours
from telemetry_nerd.analysis.quantile import attach_counts
from telemetry_nerd.analysis.reference import reference_window
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.analysis.samples import characteristic_range, pool, scan_series
from telemetry_nerd.analysis.sources import measurement_items
from telemetry_nerd.catalog.family_query import SLOT, FamilyQueryRefused
from telemetry_nerd.catalog.family_query import rewrite as rewrite_families
from telemetry_nerd.catalog.mergeability import NONMERGEABLE_CAVEAT
from telemetry_nerd.catalog.profiles import ProfileStore
from telemetry_nerd.charts.context_lines import (
    ContextSpec,
    headroom,
    line_expr,
    percent_of_limit,
    swap_metric,
)
from telemetry_nerd.charts.dataview import SignalViews, offered_views
from telemetry_nerd.charts.derived_bounds import derive_bounds
from telemetry_nerd.charts.series_cut import RANK_TEXT, cut_note, member_label, names, rank, split
from telemetry_nerd.charts.spec import (
    LINE_SERIES_BUDGET,
    SPECTRAL_MARKS,
    WINDOW_MARKS,
    AssertedBounds,
    AutoForm,
    ChartSpec,
    GroupRef,
    Layer,
    Marginal,
    Reference,
    ValidationIssue,
    Window,
    YContext,
    YLimit,
    YProfile,
    YReframe,
    YTypical,
    auto_spec,
    validate,
)
from telemetry_nerd.charts.unit_check import check_unit
from telemetry_nerd.charts.units import (
    infer_unit_with_provenance,
    metric_names,
    nonmergeable_uses,
    raw_counters,
)
from telemetry_nerd.charts.ycontext import (
    NATURAL,
    counter_rate_metric,
    counter_rate_parts,
    natural_range,
    selector_parts,
)
from telemetry_nerd.charts.yview import value_stats
from telemetry_nerd.core import fleet_payloads
from telemetry_nerd.core.binding_ops import BindingOps
from telemetry_nerd.core.card_payload import (
    MAX_METRICS,
    gap_pct,
    profile_card,
)
from telemetry_nerd.core.code_ops import CodeOps, build_runs
from telemetry_nerd.core.code_outputs import (
    code_caveat,
    refuse_estimate,
    refuse_requery,
)
from telemetry_nerd.core.entity_ops import EntityOps
from telemetry_nerd.core.events import Actor, EventLog
from telemetry_nerd.core.fleet_ops import FleetOps
from telemetry_nerd.core.littles_ops import LittlesOps
from telemetry_nerd.core.panel_payloads import (
    ghost_payload,
    heatmap_panel_data,
    histogram_panel_data,
    index_payload,
    limit_payload,
    marginal_payload,
    normal_payload,
    rug_payload,
    series_labels,
    series_payload,
    signal_payload,
    without_data,
)
from telemetry_nerd.core.presence import PresenceRegistry
from telemetry_nerd.core.profiles import ProfileService
from telemetry_nerd.core.seasonal_dist_ops import SeasonalDistOps
from telemetry_nerd.core.seasonal_ops import SeasonalOps, seasonal_hint
from telemetry_nerd.core.series_diagnostics import SeriesDiagnostics, resolve_baseline
from telemetry_nerd.core.signal_ops import SignalOps
from telemetry_nerd.core.summary import summarize, summarize_distribution
from telemetry_nerd.core.uncertainty import mark_statistics
from telemetry_nerd.core.verdict_ops import VerdictOps
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.core.workspaces import WorkspaceOps
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore, Lineage, is_code_expr
from telemetry_nerd.exchange.fmt import NO_UNCERTAINTY, UNCERTAINTY_STATUS
from telemetry_nerd.kernels.manager import KernelManager
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, coarsen, grid
from telemetry_nerd.model.caveats import (
    Caveat,
    Where,
    from_bucket_state,
    interval_caveats,
    runs,
    series_name,
)
from telemetry_nerd.model.companions import dataset_bundle
from telemetry_nerd.model.distribution import DIST_N_MIN
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.series import BUCKET_SCHEMA, FetchResult
from telemetry_nerd.model.time import (
    TimeRange,
    format_duration,
    iso,
    now_ms,
    parse_duration,
    parse_time,
)
from telemetry_nerd.sources.base import LimitExceeded, Source, SourceError, SourceUnavailable
from telemetry_nerd.sources.registry import SourceRegistry
from telemetry_nerd.sources.spec import RESERVED_NAMES, SourceSpec
from telemetry_nerd.workspace.models import PanelGroup
from telemetry_nerd.workspace.registry import WorkspaceRegistry
from telemetry_nerd.workspace.scope import ActiveWorkspace
from telemetry_nerd.workspace.store import Panel, WorkspaceStore

_NICE_STEPS = [parse_duration(s) for s in ("1s", "2s", "5s", "10s", "15s", "30s", "1m", "2m",
                                            "5m", "10m", "15m", "30m", "1h", "2h", "6h", "12h",
                                            "1d")]  # fmt: skip


def auto_step(rng: TimeRange, resolution_ms: int, target_buckets: int = 600) -> int:
    wanted = max((rng.end_ms - rng.start_ms) / target_buckets, resolution_ms)
    return next((s for s in _NICE_STEPS if s >= wanted), _NICE_STEPS[-1])


MAX_BUCKETS_PER_QUERY = 50_000
RESOLUTION_RETRY_MS = 60_000  # an unlearned source re-probes its scrape spacing at most this often
_logger = logging.getLogger(__name__)
DEFAULT_SCAN = 25
MAX_SCAN = 100  # queries per scan call: a scan is never "all metrics"
SCAN_BUDGET_S = 60.0
SCAN_POINTS = 120  # samples per series aimed for
SCAN_CANDIDATES = 20  # prefix candidates considered per allowed query (many may be fresh)
SCAN_FRESH_MS = 86_400_000
MIN_SCAN_WINDOW_MS = 300_000
MAX_SCAN_WINDOW_MS = 6 * 3_600_000
SCRAPE_CACHE_MS = 3_600_000  # a measured scrape interval is reused for an hour
PROFILE_WAIT_S = 8.0  # how long `show` waits for a first-view operating profile
DAY_MS = 86_400_000
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


def _refuse_derived_rescope(panel_id: str, meta: DatasetMeta, spec: ChartSpec) -> None:
    """A filter output keeps its parent's expr: re-querying that expr over a new range would
    silently show the raw, unfiltered signal under the same question (bead aqk)."""
    if meta.derived is not None or spec.signal is not None:
        raise ValueError(
            f"rescope_unsupported_for_derived: {panel_id} is a derived/filtered panel; rescope "
            "does not yet re-run its derivation (hint: create a new panel over the new range "
            "and re-apply the filter)"
        )


def _bounds_text(lo: float | None, hi: float | None) -> str:
    """The catalog's notation where it has one, else an interval."""
    for text, rng in NATURAL.items():
        if rng == (lo, hi) and text != "none":
            return text
    return f"[{'-∞' if lo is None else f'{lo:g}'}, {'∞' if hi is None else f'{hi:g}'}]"


def absence_note(source: str, expr: str, rng: object) -> str:
    """What an empty result does and does not show (bead rbz): never a basis for "X does not
    exist" on its own."""
    span = f" over {rng[0]}..{rng[1]}" if isinstance(rng, list | tuple) and len(rng) == 2 else ""
    return (
        f"no series matched `{expr}` in source {source!r}{span}. Absence of evidence is not "
        "evidence of absence: this metric, its label names or values may not be how this source "
        "reports the entity. Before saying a service or signal does not exist, run `entities` "
        "(which services report which metric families) and catalog_search, and cite them."
    )


@dataclass
class TelemetryService:
    sources: SourceRegistry
    cache: SeriesCache
    datasets: DatasetStore
    workspace: WorkspaceStore
    log: EventLog
    ws: WorkspaceService
    #: the daemon's active workspace; the scope every workspace store reads (spec D4)
    active: ActiveWorkspace
    registry: WorkspaceRegistry
    clock: Callable[[], int] = now_ms
    presence: PresenceRegistry = field(default_factory=PresenceRegistry)
    signal: SignalOps = field(init=False)
    diagnostics: SeriesDiagnostics = field(init=False)
    profiles: ProfileService = field(init=False)
    seasonal: SeasonalOps = field(init=False)
    fleets: FleetOps = field(init=False)
    littles: LittlesOps = field(init=False)
    _scrape_cache: dict = field(default_factory=dict, init=False, repr=False)
    #: source name -> clock ms of the last resolution probe (unlearned sources retry lazily)
    _resolution_tried: dict = field(default_factory=dict, init=False, repr=False)
    #: compute a T1 operating profile in the background when a time-series panel is shown
    auto_profile: bool = False
    #: tier-2 kernels (spec §5.2); None when tier-2 is not wired (tests, tools)
    kernels: KernelManager | None = None
    #: tier-2 run directories (<data_dir>/runs); None when tier-2 is not wired
    runs_root: Path | None = None
    code: CodeOps = field(init=False)
    bindings: BindingOps = field(init=False)
    verdicts: VerdictOps = field(init=False)
    entity_index: EntityOps = field(init=False)
    workspaces: WorkspaceOps = field(init=False)

    def __post_init__(self) -> None:
        self.ws.current = self._current_workspace
        self.workspaces = WorkspaceOps(
            self.registry, self.active, self.log, self.sources, self.source_connect,
            self.ws.open_threads,
        )  # fmt: skip
        self.bindings = BindingOps(self)
        self.verdicts = VerdictOps(self)
        self.entity_index = EntityOps(
            self._source, self.clock, self.ws.binding_suggest, self.ws.workspace.record_listing
        )
        self.signal = SignalOps(self.datasets, self.ws.catalog_facts)
        self.diagnostics = SeriesDiagnostics(
            self.signal, lambda *a: self.profiles.seasonal_excluding(*a), self.query
        )
        self.profiles = ProfileService(
            self.sources, self.cache, ProfileStore(self.workspace.connection), self.ws, self.clock
        )
        self.seasonal = SeasonalOps(self.datasets, self.signal, self.query, self._profile_periods)
        self.seasonal_dist = SeasonalDistOps(self.datasets, self.query_distribution)
        self.fleets = FleetOps(self.datasets, self.signal, self.ws.catalog_facts)
        self.littles = LittlesOps(
            self.datasets, self.query, lambda s: self._source(s).resolution_ms,
            self.ws.catalog_facts, self.ws.catalog.has_metric,
            lambda s: bool(self.ws.catalog.names(s, None, 1)), self._littles_binding, self.clock,
            self._histogram_family, lambda s: getattr(self._source(s), "flavor", None),
            lambda s, sel, at: self._source(s).scrape_interval(sel, at),
        )  # fmt: skip
        self.code = CodeOps(
            self.datasets, self.ws, self.log, self.kernels,
            build_runs(self.datasets, self.ws, self.runs_root), self.clock,
            scope=self.active, workspace_ids=self.registry.ids, using=self.active.using,
        )  # fmt: skip

    def _current_workspace(self) -> dict:
        info = self.registry.get(self.active())  # the pinned workspace, not the live pointer
        return {"id": info.id, "title": info.title, "question": info.question}

    def _littles_binding(self, source: str, key: str):
        found = self.ws.relations.bindings("catalog", source, kind="littles_law")
        hit = [b for b in found if b.key == key]
        if hit:
            return hit[0]
        keys = ", ".join(repr(b.key) for b in found) or "none yet"
        raise ValueError(
            f"no littles_law binding with key {key!r} on {source!r} (bound keys: {keys}; a "
            "binding_suggest id is not a key: binding_accept it, then pass its key; or pass "
            "arrival_rate, latency and concurrency)"
        )

    def _histogram_family(self, source: str, metric: str) -> list[str] | None:
        if not self.ws.catalog.has_metric(source, metric):
            return None
        h = self.ws.catalog_entry(source, metric).fields.get("histogram_family")
        return list(h.value) if h is not None and h.value else None

    async def check_littles_law(self, actor: Actor = "claude", **kw) -> dict:
        """L vs lambda W per window and group, with a propagated interval (czt.2)."""
        await self.ensure_resolution(kw.get("source", "default"))
        return await self.littles.check(actor=actor, **kw)

    def _profile_periods(self, source: str, expr: str) -> list[str]:
        p = self.profiles.cached(source, expr)
        if p is None:
            return []
        return sorted({s.seasonal.period for s in p.series if s.seasonal is not None})

    async def compare_seasonal(
        self,
        dataset_id: str,
        cycles: list[str] | None = None,
        tz: str = "UTC",
        exclude: list[str] | None = None,
        actor: Actor = "claude",
        threshold: float | None = None,
    ) -> dict:
        """Now vs the same phase of previous cycles, band from their spread (lkn.2); latency
        (percentile series or distributions): the histogram per cycle (lkn.7)."""
        meta = self.datasets.meta(dataset_id)
        refuse_estimate(self.datasets, meta, "compare_seasonal")
        refuse_requery(
            meta, "compare_seasonal (previous cycles of the same query)",
            "give the code the previous cycles as inputs and compare there, or compare_seasonal "
            "an input dataset",
        )  # fmt: skip
        if SeasonalDistOps.applies(meta):
            out = await self.seasonal_dist.compare(
                dataset_id, cycles, tz, exclude, threshold, actor
            )
            # the statistics rest on the dataset, the fresh "now" fetch and the cycles that
            # feed the band (kept ones; excluded cycles do not, and a series missing from a
            # cycle has a placeholder without a dataset): any of unknown uncertainty makes them
            # a lower bound (8qt)
            used = [dataset_id, out["histogram"]["now"]] + [
                c["dataset"]
                for s in out["series"]
                for c in s["reference"]["cycles"]
                if c.get("dataset")
            ]
            before = set(out["caveats"])
            mark_statistics(out, self.datasets, used)
            for c in set(out["caveats"]) - before:  # spec 5.4: the measurement system's variation
                for s in out["series"]:
                    s["variation"] += measurement_items([c])
            return out
        cfg = await self.seasonal.fetch(dataset_id, cycles, tz, exclude, actor)
        # the band is the cycles' own spread; the input's declared error is not folded in
        return mark_statistics(self.seasonal.summary(dataset_id, cfg), self.datasets, [dataset_id])

    def fleet(
        self,
        dataset_id: str,
        by: list[str] | None = None,
        scale: str = "auto",
        normalise: str = "none",
        band_window: int | None = None,
    ) -> dict:
        """Many series of one metric as a group: spread, outlying members, churn (lkn.3)."""
        out = self.fleets.summary(dataset_id, by, scale, normalise, band_window)
        return mark_statistics(out, self.datasets, [dataset_id])

    def seasonal_suggestion(self, dataset_id: str, mark: str = "auto") -> str | None:
        """Hint for `show`: the cached operating profile has a daily/weekly seasonal model."""
        meta = self.datasets.meta(dataset_id)
        if mark != "auto" or meta.representation != "bucket_agg" or meta.derived or meta.code_node:
            return None
        periods = self._profile_periods(meta.source, meta.expr)
        if not {"hour_of_day", "hour_of_week"} & set(periods):
            return None
        cyc = "week" if "hour_of_week" in periods else "day"
        return (
            f"the operating profile is seasonal ({', '.join(periods)}): ask whether now is "
            f'unusual for this time of {cyc} with compare_seasonal("{dataset_id}")'
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

    def _expand_families(self, expr: str, source: str, src: Source) -> str:
        """A name-template family written where a metric goes selects all its members and exposes
        the name-encoded text as the `dimension` label (bead 2as.26)."""
        if SLOT not in expr:
            return expr
        keeps = getattr(src, "flavor", "prometheus") == "victoriametrics"
        try:
            out, _ = rewrite_families(
                expr,
                lambda t: self.ws.families.info(source, t) is not None,
                keeps_names=keeps,
            )
        except FamilyQueryRefused as e:
            raise SourceError(str(e), hint="see the catalog family for the members") from e
        return out

    async def query(
        self,
        expr: str,
        start: str | None = None,
        end: str | None = None,
        step: str = "auto",
        source: str = "default",
        actor: Actor = "claude",
        allow_nonmergeable: bool = False,
    ) -> dict:
        if is_code_expr(expr):
            raise SourceError(
                f"{expr.strip()} names a code output (fixed data), not a source query",
                hint="pass the output's dataset handle to show or the op instead of its expr",
            )
        start = start or self.get_default_range()
        end = end or "now"
        src = self._source(source)
        await self.ensure_resolution(source)
        self.workspaces.note_source(source)
        expr = self._expand_families(expr, source, src)
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
        violations = nonmergeable_uses(
            expr, lambda m: self.ws.catalog_facts(src.name, m), override=allow_nonmergeable
        )
        if violations and not allow_nonmergeable:
            v = violations[0]
            raise SourceError(
                f"{v.metric}: {v.reason}",
                hint="recompute from the merged histogram/raw data, or pass "
                "allow_nonmergeable=true to chart it anyway with a caveat",
            )
        nonmergeable_caveats = sorted({v.caveat for v in violations if v.caveat})  # override only
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
            semantics_flags=_semantics_flags(src),
        )
        summary = self._time_summary(meta, result, now)
        if summary.get("series_count") == 0:
            summary["empty_result"] = absence_note(src.name, expr, summary.get("range"))
        if nonmergeable_caveats:  # a short code for the vocabulary; the argument is the payload
            summary["caveats"].append(NONMERGEABLE_CAVEAT)
            summary["nonmergeable"] = {
                "uses": [f"{v.op}({v.metric})" for v in violations],
                "explanation": nonmergeable_caveats[0],
            }
        self.log.append(actor, "dataset.created", meta.id, {"expr": expr})
        return {"dataset": meta.id, "summary": summary}

    def get_default_range(self) -> str:
        """The `start` new panels default to when the caller doesn't say (bead aqk)."""
        return self.workspace.get_setting("default_range", "now-1h")

    def set_default_range(self, value: str) -> str:
        now = self.clock()
        if parse_time(value, now) >= now:  # parse_time raises ValueError if unparseable
            # every defaulted query ends at now: a start at or after it would fail end <= start
            raise ValueError(f"default range {value!r} must start before now (e.g. now-1h)")
        self.workspace.set_setting("default_range", value)
        return value

    def _time_summary(self, meta, result, now: int, bundle=None) -> dict:
        bundle = bundle or dataset_bundle(self.datasets, meta, result)
        # the summary judges coverage from the bundle's states (carried through filters);
        # where coverage is not tracked there is nothing to judge, not a fresh guess
        states = bundle.companions.get("bucket_state")
        summary = summarize(
            meta, result, now_ms=now, settle_ms=self.cache.settle_ms,
            states=STATE_SCHEMA.empty_table() if states is None else states,
        )  # fmt: skip
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
        await self.ensure_resolution(source)
        self.workspaces.note_source(source)
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
            refuse_requery(meta, "a distribution of the selection")
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
        if (
            baseline == "previous"
            and meta.histogram
            and start_ms - span < meta.start_ms - meta.step_ms
        ):
            # the dataset does not reach back far enough for the comparison that was asked for:
            # widen it to cover the previous window instead of silently dropping the baseline
            out = await self.query_distribution(
                meta.histogram["selector"], meta.histogram["by"], start=str(start_ms - span),
                end=str(meta.end_ms), step=format_duration(meta.step_ms), source=meta.source,
                actor=actor,
            )  # fmt: skip
            meta = self.datasets.meta(out["dataset"])
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
        return mark_statistics(
            {"dataset": dataset_id, "x": x, "series": out}, self.datasets, [dataset_id]
        )

    async def scan_metrics(
        self,
        source: str,
        metrics: list[str] | None = None,
        prefix: str | None = None,
        limit: int = DEFAULT_SCAN,
        window: str = "30m",
        refresh: bool = False,
        budget_s: float = SCAN_BUDGET_S,
        actor: Actor = "system",
    ) -> dict:
        """T1 sample statistics for a bounded set of catalogued metrics (bead 2as.6).

        Never "all metrics": explicit names, else a prefix (hot first), else this workspace's hot
        metrics; at most MAX_SCAN queries per call, a wall-clock budget, metrics scanned in the
        last day skipped, and a metric above the source's series cap skipped with the reason."""
        src = self._source(source)
        window_ms = parse_duration(window)
        if not MIN_SCAN_WINDOW_MS <= window_ms <= MAX_SCAN_WINDOW_MS:
            raise ValueError(
                f"window must be between {format_duration(MIN_SCAN_WINDOW_MS)} and "
                f"{format_duration(MAX_SCAN_WINDOW_MS)}: a short window only suggests behaviour; "
                "long-range shape is the operating profile's job"
            )
        limit = max(1, min(limit, MAX_SCAN))
        res = max(src.resolution_ms, 1)
        step_ms = -(-max(res, window_ms // SCAN_POINTS) // res) * res  # whole resolutions
        skipped: list[dict] = []
        if metrics:
            targets = []
            for m in dict.fromkeys(metrics):
                if self.ws.catalog.has_metric(source, m):
                    targets.append(m)
                else:
                    skipped.append(
                        {"metric": m, "reason": "not in the catalog; run source_learn first"}
                    )
        else:
            hot = self.ws.catalog_hot(source)
            if prefix:
                names = self.ws.catalog.names(source, prefix, limit * SCAN_CANDIDATES)
                targets = sorted(names, key=lambda n: (n not in hot, n))
            else:
                targets = sorted(hot)
        scanned: list[dict] = []
        failed: list[dict] = []
        t0 = time.monotonic()
        attempted, stopped, left = 0, None, 0
        for i, metric in enumerate(targets):
            if self._classic_base(source, metric):
                skipped.append(
                    {
                        "metric": metric,
                        "reason": "classic histogram base name: not a series; scan its "
                        f"{metric}_count or {metric}_sum (query_distribution for its buckets)",
                    }
                )
                continue
            prev = self.ws.samples.get(source, metric)
            if prev is not None and not refresh and self.clock() - prev.scanned_ms < SCAN_FRESH_MS:
                skipped.append(
                    {
                        "metric": metric,
                        "reason": "scanned within the last day (refresh=true to redo)",
                    }
                )
                continue
            if attempted >= limit:
                stopped, left = "limit", len(targets) - i
                break
            if time.monotonic() - t0 >= budget_s:
                stopped, left = "budget", len(targets) - i
                break
            attempted += 1
            try:
                out = await self.query(
                    metric,
                    start=f"now-{format_duration(window_ms)}",
                    end="now",
                    step=format_duration(step_ms),
                    source=source,
                    actor=actor,
                )
            except LimitExceeded as e:
                skipped.append({"metric": metric, "reason": str(e)})
                continue
            except (SourceError, ValueError) as e:
                failed.append({"metric": metric, "reason": str(e)})
                continue
            ds = out["dataset"]
            _, result = self.datasets.get(ds)
            stats = pool(_series_stats(result.buckets))
            vrange = characteristic_range(result.buckets["avg"].to_pylist())
            done = self.ws.record_scan(
                source, metric, stats, ds, window_ms, step_ms, actor, value_range=vrange
            )
            scanned.append(
                {
                    "metric": metric, "verdict": stats.verdict, "series": stats.series,
                    "samples": stats.n, "increases": stats.increases, "resets": stats.resets,
                    "small_decreases": stats.small_decreases, "negatives": stats.negatives,
                    "range": (
                        {
                            "p1": vrange.lo, "p99": vrange.hi, "min": vrange.min,
                            "max": vrange.max, "p1_ci95": list(vrange.lo_ci),
                            "p99_ci95": list(vrange.hi_ci),
                        }
                        if vrange else None
                    ),
                    **done,
                }
            )  # fmt: skip
        return {
            "source": source, "window": format_duration(window_ms), "step": format_duration(step_ms),
            "scanned": scanned, "skipped": skipped, "failed": failed,
            "stopped": stopped, "remaining": left,
        }  # fmt: skip

    def _classic_base(self, source: str, metric: str) -> bool:
        """X of a classic histogram (X_bucket/_sum/_count): catalogued, but no series is named X."""
        h = self.ws.catalog_entry(source, metric).fields.get("histogram_family")
        return h is not None and f"{metric}_bucket" in h.value

    async def panel_card(self, panel_id: str) -> dict:
        """The metric card for a panel (bead 2as.12): catalog claims with provenance for each
        catalogued metric in the expression, the operating profile and measurable data quality."""
        p = self.workspace.get_panel(panel_id)
        meta, result = self.datasets.get(p.dataset_ids[0])
        if meta.code_node:
            return self._code_card(meta, result)
        parts = selector_parts(meta.expr)
        names = sorted(metric_names(meta.expr), key=lambda n: (parts is None or n != parts[0], n))
        known = [n for n in names if self.ws.catalog.has_metric(meta.source, n)][:MAX_METRICS]
        metrics = [self.ws.metric_section(meta.source, name) for name in known]
        return {
            "source": meta.source,
            "metrics": metrics,
            "learned": bool(metrics),
            "profile": await self._card_profile(meta),
            "quality": await self._card_quality(meta, result, known[0] if known else None, parts),
        }

    def _code_card(self, meta: DatasetMeta, result: FetchResult) -> dict:
        """Card of a code output: no catalog metrics, profile or scrape interval to look up (its
        expression names an output, not metrics); what the dataset itself says."""
        why = "code output: fixed data, not scraped from a source"
        series = self.datasets.series_count(meta.id)
        return {
            "source": meta.source,
            "metrics": [],
            "learned": False,
            "produced_by": {**(meta.producer or {}), "parents": list(meta.parents)},
            "profile": {"available": False, "reason": f"{why}: no operating profile"},
            "quality": {
                "step_ms": meta.step_ms, "resolution_ms": meta.resolution_ms,
                "scrape_interval_ms": None, "scrape_interval_reason": why, "series": series,
                "gap_pct": gap_pct(result.buckets, meta.start_ms, meta.end_ms, meta.step_ms),
                "resets": {"measured": False, "reason": why},
                "cardinality": {"in_panel": series, "catalog": None},
            },
        }  # fmt: skip

    async def _card_profile(self, meta: DatasetMeta) -> dict:
        have = self.profiles.cached(meta.source, meta.expr)
        if have is not None:
            return profile_card(have)
        try:
            self.profiles.target(meta.source, meta.expr)
        except SourceError as e:
            return {"available": False, "reason": str(e)}
        self.profiles.request(meta.source, meta.expr)  # compute in the background
        return {"available": False, "reason": "not computed yet (it is being computed now)"}

    def _card_resets(self, source: str, metric: str | None) -> dict:
        obs = self.ws.samples.get(source, metric) if metric else None
        if obs is None:
            return {
                "measured": False,
                "reason": "not scanned yet (catalog_scan measures it over a short window)",
            }
        return {
            "measured": True, "window_ms": obs.window_ms, "series": obs.series, "samples": obs.n,
            "resets": obs.resets, "small_decreases": obs.small_decreases, "negatives": obs.negatives,
            "verdict": obs.verdict, "scanned_ms": obs.scanned_ms,
        }  # fmt: skip

    async def _card_quality(self, meta, result, metric: str | None, parts) -> dict:
        interval, why = None, None
        selector = meta.expr if parts is not None else metric
        if selector is None:
            why = "the expression is not a single metric"
        else:
            hit = self._scrape_cache.get((meta.source, selector))
            if hit is not None and self.clock() - hit[0] < SCRAPE_CACHE_MS:
                interval, why = hit[1], hit[2]
            else:
                try:
                    interval = await self._source(meta.source).scrape_interval(selector)
                    why = None if interval else "fewer than 3 recent samples"
                except SourceError as e:
                    why = str(e)
                self._scrape_cache[(meta.source, selector)] = (self.clock(), interval, why)
        series = self.datasets.series_count(meta.id)
        return {
            "step_ms": meta.step_ms,
            "resolution_ms": meta.resolution_ms,
            "scrape_interval_ms": interval,
            "scrape_interval_reason": why,
            "series": series,
            "gap_pct": gap_pct(result.buckets, meta.start_ms, meta.end_ms, meta.step_ms),
            "resets": self._card_resets(meta.source, metric),
            "cardinality": {"in_panel": series, "catalog": None},
        }

    async def show_auto(
        self,
        dataset_id: str,
        question: str,
        actor: Actor = "claude",
        raw: bool = False,
        **kw,
    ) -> ShowResult:
        """`show`, choosing the form of the signal from the catalog (bead 2as.14).

        A plain selector of a counter is a running total: draw its rate instead, from a new dataset
        over the same window and step, and say so on the panel. `raw=True` draws what was asked."""
        meta = self.datasets.meta(dataset_id)
        auto_ok = (
            not raw
            and kw.get("mark", "auto") == "auto"
            and not meta.derived
            and meta.representation == "bucket_agg"
            and meta.code_node is None  # fixed data: drawn as produced, never re-queried
            and selector_parts(meta.expr) is not None
        )
        if auto_ok:
            metric = selector_parts(meta.expr)[0]  # type: ignore[index]
            if self.ws.catalog_facts(meta.source, metric).type == "counter":
                try:
                    rate = (
                        await self.query(
                            f"rate({meta.expr.strip()}[{RATE_INTERVAL}])",
                            start=str(meta.start_ms),
                            end=str(meta.end_ms),
                            step=format_duration(meta.step_ms),
                            source=meta.source,
                            actor=actor,
                        )
                    )["dataset"]
                except (SourceError, ValueError):
                    pass  # falls through to the raw chart, with the raw_counter warning
                else:
                    form = AutoForm(
                        transform="rate",
                        source_dataset=dataset_id,
                        reason=f"{metric} is a counter (a running total); its per-second rate is drawn",
                    )
                    return self.show(rate, question, actor, auto=form, **kw)
        return self.show(dataset_id, question, actor, raw_ok=raw, **kw)

    async def show_binding(self, **kw) -> PanelGroup:
        """A bound USE / RED / Little's law metric set as one linked panel group (bead czt.3)."""
        await self.ensure_resolution(kw.get("source", "default"))
        return await self.bindings.show(**kw)

    async def binding_verdict(self, actor: Actor = "claude", **kw) -> dict:
        """Per-signal verdicts for a binding or panel group against reference windows (czt.4)."""
        await self.ensure_resolution(kw.get("source") or "default")
        return await self.verdicts.verdict(actor=actor, **kw)

    async def reframe_group(
        self, group_id: str, start_ms: int, end_ms: int, actor: Actor = "user"
    ) -> PanelGroup:
        """The same panel group over another window (a selection): a NEW group, marked as
        reframed from this one, which stays as it was."""
        g = self.ws.group_get(group_id)
        return await self.bindings.show(
            source=g.source, kind=g.kind, key=g.key,
            suggestion=g.suggestion if g.basis == "suggestion" else None,
            start=str(start_ms), end=str(end_ms), matchers=g.matchers,
            error_matcher=g.error_matcher, actor=actor, reframed_from=g.id,
        )  # fmt: skip

    async def preview(self, panel_id: str, start: str, end: str, actor: Actor = "user") -> dict:
        """A dataset over a different range for `panel_id`, without touching it (bead aqk):
        the server side of a client-side zoom preview. Nothing is persisted or logged beyond
        the routine internal dataset.created event any fetch makes."""
        p = self.workspace.get_panel(panel_id)
        meta = self.datasets.meta(p.dataset_ids[0])
        refuse_requery(meta, "a time-range preview")
        _refuse_derived_rescope(panel_id, meta, ChartSpec.model_validate(p.spec))
        # step=auto: the old step was chosen for the old range (a 15 m panel's step over 7 d
        # would be tens of thousands of buckets)
        return await self.query(
            meta.expr, start=start, end=end, step="auto", source=meta.source, actor=actor
        )

    async def rescope(
        self, panel_id: str, start: str, end: str, actor: Actor = "user"
    ) -> ShowResult:
        """Accept a time-range change (bead aqk): a NEW panel over the new range, marked as
        rescoped from this one, which is left exactly as it was. Never applied silently."""
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        mark = spec.layers[0].mark if spec.layers else "auto"
        if mark not in ("line+envelope", "fleet", "spc"):
            raise ValueError(
                f"rescope_unsupported_for_mark: {panel_id} is a {mark} panel; rescope only "
                "supports line+envelope, fleet and spc panels for now (hint: create a new "
                "panel over the new range instead)"
            )
        meta = self.datasets.meta(p.dataset_ids[0])
        refuse_requery(meta, "a time-range rescope")
        _refuse_derived_rescope(panel_id, meta, spec)
        layer = spec.layers[0]
        if mark == "spc" and not layer.windows and layer.spc is not None:
            # the baseline is a separately fetched reference (analyze_reference), not a window:
            # show() would silently auto-pick an in-dataset baseline for the new dataset
            raise ValueError(
                f"rescope_unsupported_for_spc_reference: {panel_id}'s spc baseline is a "
                "reference comparison, not an explicit window; rescope does not yet re-derive "
                "it (hint: create a new panel and re-run the reference comparison)"
            )
        # step=auto: the old step was chosen for the old range, not this one (reframe keeps the
        # window, so it keeps the step; a rescope changes the window)
        ds = (
            await self.query(
                meta.expr, start=start, end=end, step="auto", source=meta.source, actor=actor
            )
        )["dataset"]
        form = AutoForm(
            transform="rescope",
            source_dataset=p.dataset_ids[0],
            reason=f"rescoped from {panel_id}",
        )
        kwargs: dict = {}
        if spec.y.unit and (spec.y.unit_provenance or "").startswith("provided by"):
            kwargs["unit"] = spec.y.unit  # an asserted unit; an inferred one is re-inferred
        if (ab := spec.y.asserted_bounds) is not None:
            kwargs.update(bounds_lo=ab.lo, bounds_hi=ab.hi, bounds_by=ab.by)
        if mark == "fleet":
            # the panel's persisted config, not FleetOps' in-memory last call (lost on restart,
            # clobbered by any other fleet() on that dataset)
            self.fleets.summary(ds, **(layer.fleet or {}))
            kwargs["mark"] = "fleet"
        elif mark == "spc":
            kwargs["mark"] = "spc"
            if layer.windows:
                w = layer.windows[0]
                kwargs["windows"] = [Window(start_ms=w.start_ms, end_ms=w.end_ms)]
        elif layer.quantiles:
            kwargs.update(mark="line+envelope", quantiles=list(layer.quantiles))
        res = self.show(ds, p.question, actor, auto=form, raw_ok=True, **kwargs)
        self.log.append(actor, "panel.rescoped", res.panel.id, {"from": panel_id})
        await self.y_context(res.panel.id, actor)
        return res

    async def reframe(self, panel_id: str, index: int, actor: Actor = "user") -> ShowResult:
        """Accept a proposed reframing (bead 2as.15): a NEW panel over the same window and step,
        marked as reframed from this one, which is left exactly as it was. Never applied silently."""
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        options = spec.y.context.reframes if spec.y.context else []
        if not 0 <= index < len(options):
            raise ValueError(
                f"panel {panel_id} has {len(options)} reframings; index {index} is out of range"
            )
        choice = options[index]
        meta = self.datasets.meta(p.dataset_ids[0])
        refuse_requery(meta, "a reframing")
        ds = (
            await self.query(
                choice.expr,
                start=str(meta.start_ms),
                end=str(meta.end_ms),
                step=format_duration(meta.step_ms),
                source=meta.source,
                actor=actor,
            )
        )["dataset"]
        form = AutoForm(
            transform="reframe",
            source_dataset=p.dataset_ids[0],
            reason=f"reframed from {panel_id}: {choice.title}. {choice.reason}",
        )
        res = self.show(
            ds, f"{p.question} ({choice.title})", actor, unit=choice.unit, auto=form, raw_ok=True
        )
        await self.y_context(res.panel.id, actor)
        return res

    async def y_context(self, panel_id: str, actor: Actor = "system") -> YContext | None:
        """Work out what the catalog says about a time panel's y axis and record it (bead 2as.10).

        Natural bounds come from the metric's catalog `bounds` claim; the physical limit is the
        `bounded_by` metric fetched under the same label matchers. Only plain selectors (and
        rate/increase of one counter) qualify: bounds of a mixed expression belong to no single
        metric. A source failure degrades to a note: the panel still renders."""
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        marks = {layer.mark for layer in spec.layers}
        fleet = marks == {"fleet"}
        if not (fleet or marks == {"line+envelope"}) or spec.signal:
            return None
        meta = self.datasets.meta(p.dataset_ids[0])
        if meta.code_node:
            return None  # no catalog metric, limit or profile behind a code output
        ctx = YContext()
        parts = selector_parts(meta.expr)
        metric = parts[0] if parts else counter_rate_metric(meta.expr)
        unit: tuple[str, str] | None = None  # a unit the bounds rule implies (never over Claude's)
        derived = None if parts else self._derived_bounds(meta)
        if spec.y.asserted_bounds is not None:
            ab = spec.y.asserted_bounds
            ctx.natural_lo, ctx.natural_hi = ab.lo, ab.hi
            ctx.bounds = _bounds_text(ab.lo, ab.hi)
            ctx.bounds_origin, ctx.bounds_basis = ab.by, f"asserted by {ab.by}"
        elif metric is not None and parts is not None:
            if found := self.ws.catalog_bounds(meta.source, metric):
                ctx.bounds, ctx.bounds_origin = found
                ctx.natural_lo, ctx.natural_hi = natural_range(found[0])
        elif derived is not None:
            ctx.natural_lo, ctx.natural_hi, ctx.bounds = derived.lo, derived.hi, derived.bounds
            ctx.bounds_origin, ctx.bounds_basis = "rule", derived.basis
            ctx.bounds_confidence = derived.confidence
            unit = (derived.unit, f"rule: {derived.basis}")
        elif metric is not None and self.ws.catalog_facts(meta.source, metric).type == "counter":
            # a rate of a counter is never negative, whatever the counter's own bounds say
            ctx.natural_lo, ctx.bounds, ctx.bounds_origin = 0.0, "≥0", "counter rate"
        if ctx.bounds is None and metric is None:
            ctx.notes.append("natural_bounds_unknown: the expression is not a single metric")
        if parts is not None:
            ctx.typical = self._typical_range(meta.source, parts[0])
        if fleet:
            # the spread band shares the members' value axis: bounds apply (unless the fleet is
            # normalised to each member's median, or log-scaled); limits and profiles are per series
            cfg = spec.layers[0].fleet or {}
            if cfg.get("normalise") == "member" and ctx.bounds is not None:
                ctx = YContext(notes=["natural_bounds_unknown: the fleet is normalised per member"])
                unit = None
            self.ws.set_y_context(p.id, ctx, actor, unit=unit)
            return ctx
        specs = self.ws.catalog_context_specs(meta.source, metric) if metric else []
        level = parts is not None  # a plain selector; otherwise a rate of a counter (or nothing)
        rate_parts = counter_rate_parts(meta.expr)
        panel_matchers = (parts or rate_parts or ("", ""))[1]
        for spec in specs:
            applies = spec.params.get("applies_to", "level")
            if metric is not None and applies == "rate" and level:
                continue  # a bound on the rate says nothing about the running total
            if applies == "level" and not level:
                if spec.kind == "limit":
                    ctx.notes.append(
                        "limit_unavailable: the physical limit bounds the metric itself, not its rate"
                    )
                continue
            if line := await self._fetch_context_line(meta, panel_matchers, spec, ctx.notes):
                ctx.lines.append(line)
        if metric is not None:
            self.ws.raise_context_gaps(meta.source, metric, "system")
            ctx.reframes = self._reframes(meta, metric, panel_matchers, specs, ctx.lines)
        ctx.profile = await self._fetch_profile(meta, ctx.notes)
        self.ws.set_y_context(p.id, ctx, actor, unit=unit)
        return ctx

    def _typical_range(self, source: str, metric: str) -> YTypical | None:
        """The catalog's observed range of a metric's values (4f1), for a y view: descriptive,
        labelled with its window and sample count, never treated as a bound."""
        e = (
            self.ws.catalog_entry(source, metric)
            if self.ws.catalog.has_metric(source, metric)
            else None
        )
        r = e.fields.get("typical_range") if e is not None else None
        if r is None or not isinstance(r.value, dict):
            return None
        v = r.value
        q = v.get("q") or [0.01, 0.99]
        basis = (
            f"p{q[0] * 100:g}–p{q[1] * 100:g} of {v['n']} samples ({v.get('series', '?')} series) "
            f"over a {v['window']} scan on {iso(r.ts_ms)[:10]}; observed, not a bound"
        )
        return YTypical(lo=float(v["lo"]), hi=float(v["hi"]), basis=basis)

    def _derived_bounds(self, meta: DatasetMeta):
        """Bounds the rule library carries for a derived expression, from catalog facts."""
        src = meta.source

        def nonneg(m: str) -> bool:
            if self.ws.catalog_facts(src, m).type == "counter":
                return True
            b = self.ws.catalog_bounds(src, m)
            return b is not None and natural_range(b[0])[0] == 0.0

        return derive_bounds(
            meta.expr,
            bounds_of=lambda m: (self.ws.catalog_bounds(src, m) or (None,))[0],
            nonneg=nonneg,
            unit_of=lambda m: self.ws.catalog_facts(src, m).unit,
            bounded_by=lambda a, b: b in self.ws.catalog_bounded_by(src, a),
        )

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

    async def _fetch_context_line(
        self, meta: DatasetMeta, panel_matchers: str, spec: ContextSpec, notes: list[str]
    ) -> YLimit | None:
        """One context line for a panel: a constant, or the target metric fetched under the
        panel's own labels over its window and step. A source failure is a note, not an error."""
        common: dict[str, Any] = {
            "metric": spec.target, "basis": spec.basis, "kind": spec.kind, "label": spec.label,
            "origin": spec.origin, "confidence": spec.confidence, "tone": spec.tone,
        }  # fmt: skip
        if spec.value is not None:
            return YLimit(hi=spec.value, value=spec.value, **common)
        note = "limit_unavailable" if spec.kind == "limit" else "context_unavailable"
        expr = line_expr(spec.target, spec.params, panel_matchers)
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
            notes.append(f"{note}: {spec.target} could not be fetched ({e})")
            return None
        if hi is not None and hi <= 0 and spec.params.get("zero_is_unlimited"):
            notes.append(
                f"{note}: {spec.target} is 0 here, which means unlimited, so no line is drawn"
            )
            return None
        if hi is None:
            notes.append(f"{note}: {spec.target} has no data under these labels")
            return None
        return YLimit(dataset=ds, hi=hi, **common)

    def _reframes(
        self,
        meta: DatasetMeta,
        metric: str,
        panel_matchers: str,
        specs: list[ContextSpec],
        lines: list[YLimit],
    ) -> list[YReframe]:
        """Ways to show the metric that carry their own context: pack substitutions, and
        used-as-a-fraction-of-its-limit where a hard limit was found for this panel."""
        out = []
        for rule in self.ws.catalog_reframes(meta.source, metric):
            if (expr := swap_metric(meta.expr.strip(), rule.metric, rule.replace_with)) is not None:
                out.append(
                    YReframe(
                        title=rule.title, reason=rule.reason, basis=rule.basis, expr=expr,
                        kind="substitute",
                    )
                )  # fmt: skip
        drawn = {(ln.metric, ln.kind) for ln in lines}
        for spec in specs:
            if (
                spec.kind != "limit"
                or spec.value is not None
                or (spec.target, "limit") not in drawn
            ):
                continue
            bound = line_expr(spec.target, spec.params, panel_matchers)
            out.append(
                YReframe(
                    title=f"show as % of {spec.label or spec.target}",
                    reason="used as a fraction of its limit needs no second axis to be read",
                    basis=f"{spec.origin} {spec.kind}: {spec.target}",
                    expr=percent_of_limit(meta.expr.strip(), bound, spec.params.get("join_on")),
                    kind="percent_of_limit",
                    unit="%",
                )
            )
            out.append(
                YReframe(
                    title=f"show headroom to {spec.label or spec.target}",
                    reason="what is left carries the limit with it: no second line to compare against",
                    basis=f"{spec.origin} {spec.kind}: {spec.target}",
                    expr=headroom(meta.expr.strip(), bound, spec.params.get("join_on")),
                    kind="headroom",
                )
            )
            break  # the strongest limit only
        return out

    async def split_outcome(self, dataset_id: str, actor: Actor = "user") -> dict:
        """Latency by outcome (bead 2as.19): the same histogram for successful and for failed
        requests as two new panels, so fast errors cannot flatter the latency and slow ones cannot
        hide in it. The outcome label and its values are found and classified deterministically
        (analysis/outcome.py); values it cannot classify (4xx, unknown words) are left out and
        reported. The original is untouched."""
        meta = self.datasets.meta(dataset_id)
        refuse_requery(meta, "split_outcome (the histogram by outcome label)")
        h = meta.histogram
        if not h:
            raise ValueError(
                "splitting by outcome needs a histogram-backed dataset (a latency distribution or "
                "a histogram_quantile series)"
            )
        src = self._source(meta.source)
        cands = candidate_labels((await src.discover()).label_names)
        if not cands:
            raise ValueError(
                "this source has no outcome-like label "
                f"({', '.join(OUTCOME_LABELS[:4])}, ...); nothing to split by"
            )
        dstep = max(meta.step_ms, 2 * src.resolution_ms)
        rng = TimeRange(meta.start_ms, meta.end_ms)
        found = None
        for label in cands:
            dist = await src.fetch_histogram(h["selector"], (label,), rng, dstep)
            if dist.failed:  # the values seen may be missing some: no definitive classification
                raise SourceUnavailable(
                    f"the outcome-label probe on `{label}` got only a partial response "
                    f"({dist.failed[0][2]}); its values cannot be classified reliably",
                    hint="retry shortly",
                )
            values = [lb[label] for lb in series_labels(dist.series).values() if lb.get(label)]
            outcomes = classify(label, values)
            if outcomes.success or outcomes.failure:
                found = outcomes
                break
        if found is None:
            raise ValueError(
                f"none of the outcome labels {cands} has classifiable values on this histogram"
            )
        out: dict = {"label": found.label, "excluded": list(found.excluded), "dataset": dataset_id}
        for kind, values in (("success", found.success), ("failure", found.failure)):
            if not values:
                out[kind] = None
                continue
            sel = add_matcher(h["selector"], found.label, values)
            what = "successful" if kind == "success" else "failed"
            question = f"How long do {what} requests take ({found.label}=~{'|'.join(values)})?"
            if meta.representation == "distribution":
                ds = (
                    await self.query_distribution(
                        sel, h["by"], start=str(meta.start_ms), end=str(meta.end_ms),
                        step=format_duration(meta.step_ms), source=meta.source, actor=actor,
                    )
                )["dataset"]  # fmt: skip
                res = self.show(ds, question, actor)
            else:
                expr = meta.expr.replace(h["selector"], sel)
                if expr == meta.expr:
                    raise ValueError("the histogram selector is not written out in the expression")
                ds = (
                    await self.query(
                        expr, start=str(meta.start_ms), end=str(meta.end_ms),
                        step=format_duration(meta.step_ms), source=meta.source, actor=actor,
                    )
                )["dataset"]  # fmt: skip
                res = self.show(ds, question, actor, raw_ok=True)
                await self.y_context(res.panel.id, actor)
            out[kind] = {"panel": res.panel.id, "dataset": ds, "values": list(values)}
        if out["failure"] is None:
            out["note"] = f"no failed requests ({found.label}) in this window: nothing to compare"
        return out

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
        refuse_requery(
            meta,
            "the operating profile" if mode == "profile" else f"a {mode} reference window",
            "give the code the reference window as an input and show both outputs, or use the "
            "indexed view with baseline=window",
        )  # fmt: skip
        if mode == "profile":
            return await self._profile_reference(meta, actor)
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

    async def _profile_reference(self, meta: DatasetMeta, actor: Actor) -> Reference:
        """The panel's normal: the operating profile's hourly values for the same series, in the
        same seasonal buckets the window covers (bead 2as.22). Refuses what it cannot compare
        like with like (percentiles, histograms, steps coarser than an hour)."""
        if meta.representation != "bucket_agg" or meta.histogram or meta.derived:
            raise ValueError(
                "a marginal against the profile compares hourly means of plain series; this panel "
                "shows percentiles, observations or a derived series: use reference=previous or week"
            )
        _, cur = self.datasets.get(meta.id)
        now_df, _ = hourly_means(cur.buckets, meta.step_ms)  # also refuses a coarse step
        profile = await self.profiles.ensure(meta.source, meta.expr)
        hourly = self.profiles.hourly(profile)
        if hourly is None:
            raise ValueError(
                "the profile's hourly history is no longer cached: refresh the profile"
            )
        ref_df, labels, series_tbl = hourly
        mine = {
            tuple(sorted((k, v) for k, v in json.loads(s).items() if k != "__name__"))
            for s in cur.series.column("labels").to_pylist()
        }
        keep = {
            sid
            for sid, lab in labels.items()
            if tuple(sorted((k, v) for k, v in lab.items() if k != "__name__")) in mine
        }
        if not keep:
            raise ValueError("the profile covers other series than this panel shows")
        by_sid = {s.series_id: s for s in profile.series}
        now_hours = now_df["hour_ms"].unique().to_numpy()
        pieces, periods = [], set()
        for sid in sorted(keep):
            g = ref_df.filter(pl.col("series_id") == sid)
            seas = by_sid[sid].seasonal if sid in by_sid else None
            period = seas.period if seas else "none"
            periods.add(period)
            hours = g["ts_ms"].to_numpy() - profile.step_ms
            mask = matched_hours(now_hours, hours, period, profile.tz)  # type: ignore[arg-type]
            pieces.append(g.filter(pl.Series(mask)))
        rows = pl.concat(pieces).sort("series_id", "ts_ms")
        if rows.height == 0:
            raise ValueError("the profile has no hours matching this window's time of day/week")
        shown = "none" if periods == {"none"} else next(iter(periods - {"none"}))
        window = format_duration(profile.rate_window_ms) if profile.rate_window_ms else None
        label = describe(shown, profile.window_ms / 86_400_000, profile.tz, window)  # type: ignore[arg-type]
        result = FetchResult(
            rows.select("ts_ms", "series_id", "avg", "min", "max", "count")
            .to_arrow()
            .cast(BUCKET_SCHEMA),
            series_tbl.filter(pc.is_in(series_tbl["series_id"], pa.array(sorted(keep)))),
        )
        ds = self.datasets.put(
            source=meta.source,
            expr=profile.expr,
            rng=TimeRange(profile.start_ms, profile.end_ms),
            step_ms=profile.step_ms,
            resolution_ms=profile.step_ms,
            result=result,
            derived={"op": "profile_reference", "from": profile.id, "label": label},
        )
        self.log.append(
            actor,
            "dataset.created",
            ds.id,
            {"expr": profile.expr, "derived": {"op": "profile_reference", "from": profile.id}},
        )
        return Reference(
            mode="profile",
            label=label,
            start_ms=profile.start_ms,
            end_ms=profile.end_ms,
            shift_ms=0,
            series=ds.id,
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
        out = self.signal.spectrum_summary(
            dataset_id,
            top,
            parse_duration(min_period) if min_period else None,
            parse_duration(max_period) if max_period else None,
        )
        found = [
            (p["period_s"], *p["interval_s"])
            for s in out["series"]
            for p in s["peaks"]
            if p["significant"]
        ]
        if hint := seasonal_hint(dataset_id, found):
            out["suggest"] = hint
        return mark_statistics(out, self.datasets, [dataset_id])

    def analyze(
        self,
        dataset_id: str,
        baseline_start: str | None = None,
        baseline_end: str | None = None,
    ) -> dict:
        """Periodicity, stability, SPC against a stated baseline, shape -> verdict (lkn.1)."""
        now = self.clock()
        out = self.diagnostics.summary(
            dataset_id,
            parse_time(baseline_start, now) if baseline_start else None,
            parse_time(baseline_end, now) if baseline_end else None,
        )
        self.diagnostics.remember(dataset_id, None)
        return mark_statistics(self._analyze_hint(dataset_id, out), self.datasets, [dataset_id])

    async def analyze_reference(
        self,
        dataset_id: str,
        baseline: str,
        cycles: int = 1,
        tz: str = "UTC",
        actor: Actor = "claude",
    ) -> dict:
        """analyze with SPC limits from a separately fetched earlier window (lkn.5)."""
        refuse_requery(
            self.datasets.meta(dataset_id), f"analyze(baseline={baseline!r})",
            "analyze it with a window baseline inside its range, or give the code the earlier "
            "window as an input",
        )  # fmt: skip
        ref = await self.diagnostics.fetch_reference(dataset_id, baseline, cycles, tz, actor)
        for d in [dataset_id, *(r["dataset"] for r in ref["refs"])]:
            await self.diagnostics.fetch_sibling(d, actor)
            await self.diagnostics.fetch_ratio(d, actor)

        def run() -> dict:
            out = self.diagnostics.summary(dataset_id, ref=ref)
            self.diagnostics.remember(dataset_id, ref)
            return self._analyze_hint(dataset_id, out)

        return await self._seasonal_centre(dataset_id, run(), run)

    async def analyze_profiled(
        self,
        dataset_id: str,
        baseline_start: str | None = None,
        baseline_end: str | None = None,
    ) -> dict:
        """analyze, computing the operating profile first when the SPC centre needs its
        seasonal shape and none is cached (telemetry-nerd-3af). A counter series born on its
        first event gets its live sibling fetched first (absence read as 0 where it reports)."""
        await self.diagnostics.fetch_sibling(dataset_id, "claude")
        await self.diagnostics.fetch_ratio(dataset_id, "claude")
        out = self.analyze(dataset_id, baseline_start, baseline_end)
        return await self._seasonal_centre(
            dataset_id, out, lambda: self.analyze(dataset_id, baseline_start, baseline_end)
        )

    async def _seasonal_centre(self, dataset_id: str, out: dict, rerun: Callable[[], dict]) -> dict:
        """A baseline shorter than 2 days cannot fit a daily/weekly cycle itself; the SPC centre
        then needs the operating profile's seasonal shape. Without a cached profile, compute it
        (bounded wait; it keeps computing in the background) and re-run, and say what happened:
        never a silent flat centre. `seasonal_centre.status`: profile (used), computed_now (just
        computed and used), not_seasonal (the profile has no cycle to follow), pending, or
        unavailable (no profile can be computed: why)."""
        meta = self.datasets.meta(dataset_id)

        def uses_profile(o: dict) -> bool:
            return any(
                "profile" in str((s.get("spc") or {}).get("centre", {}).get("seasonal") or "")
                for s in o.get("series", [])
            )

        def flag(o: dict, status: str, reason: str, caveat: str | None = None) -> dict:
            o["seasonal_centre"] = {"status": status, "reason": reason}
            if caveat and caveat not in o.setdefault("caveats", []):
                o["caveats"].append(caveat)
            return o

        if uses_profile(out):
            return flag(out, "profile", "the operating profile's seasonal shape is the centre")
        if meta.code_node or meta.derived or self.profiles.cached(meta.source, meta.expr):
            return out  # no profile applies, or the cached one was already considered
        if _baseline_cover_ms(out.get("baseline") or {}) >= 2 * DAY_MS:
            return out  # the baseline fits a daily cycle itself
        try:
            self.profiles.target(meta.source, meta.expr)
            prof = await asyncio.wait_for(
                self.profiles.ensure(meta.source, meta.expr), PROFILE_WAIT_S
            )
        except TimeoutError:
            return flag(
                out, "pending",
                "the baseline spans < 2 days and the operating profile (its seasonal shape) is "
                "still being computed: the centre is flat for now; re-run analyze shortly",
                "seasonal_profile_pending",
            )  # fmt: skip
        except (SourceError, ValueError) as e:
            return flag(
                out, "unavailable",
                f"the baseline spans < 2 days and no operating profile can be computed ({e}): "
                "a daily/weekly cycle is not modelled in the centre",
                "seasonal_profile_unavailable",
            )  # fmt: skip
        new = rerun()
        history = format_duration(prof.window_ms)
        if uses_profile(new):
            return flag(
                new, "computed_now",
                f"the operating profile ({history}) was computed for this analysis; its "
                "seasonal shape is the centre",
            )  # fmt: skip
        return flag(
            new, "not_seasonal",
            f"the operating profile ({history}) models no daily/weekly cycle for these series "
            "(none found, or too little history to fit one): the centre is flat",
        )  # fmt: skip

    def _analyze_hint(self, dataset_id: str, out: dict) -> dict:
        found = [
            (p["period_s"], *p["interval_s"])
            for s in out["series"]
            for p in s.get("frequency", {}).get("periods", [])
        ]
        if hint := seasonal_hint(dataset_id, found):
            out["suggest"] = hint
        return out

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
        lineage = None
        if meta.code_node:
            # still the code's output (fixed data, its unit), now filtered: a declared interval
            # or exactness does not survive a filter, so its uncertainty is unknown (spec §5.3:
            # citable, flagged); the parent's own status is replaced, not stacked
            caveats = [c for c in meta.source_caveats if c not in UNCERTAINTY_STATUS]
            caveats.append(NO_UNCERTAINTY)
            lineage = Lineage(
                producer=dict(meta.producer or {}), parents=(meta.id,), unit=meta.unit,
                caveats=tuple(caveats),
            )  # fmt: skip
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
            lineage=lineage,
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
        self._resolution_tried.pop(spec.name, None)
        res = await self.learn_resolution(spec.name)
        public = spec.public()
        self.log.append(actor, "source.connected", spec.name, {"source": public})
        out = {"source": public, "status": status}
        if res is not None:
            out["resolution"] = res
        return out

    def source_list(self) -> list[dict]:
        out = self.sources.describe()
        for entry in out:
            info = getattr(self.sources.get(entry["name"]), "resolution_info", None)
            if info is not None:
                entry["resolution"] = info()
        return out

    async def source_status(self, name: str) -> dict:
        source = self.sources.get(name)
        if source is None:
            raise SourceError(f"unknown source {name!r}", hint="see source_list for names")
        try:
            out = await source.probe()
        except SourceError as e:
            return {"reachable": False, "error": str(e), "hint": e.hint}
        if getattr(source, "resolution_origin", None) == "assumed":
            self._resolution_tried.pop(name, None)  # a status check is a good time to retry
        if (res := await self.learn_resolution(name, once=True)) is not None:
            out["resolution"] = res
        return out

    async def learn_resolution(self, name: str, once: bool = False) -> dict | None:
        """Measure the source's scrape spacing and use it as its resolution unless one is
        configured (bead wbw). once: skip when already learned or tried. None for sources that
        cannot measure it (replays, fakes). A failed probe leaves the resolution as it was."""
        src = self.sources.get(name)
        learn = getattr(src, "learn_resolution", None)
        if learn is None:
            return None
        if once and name in self._resolution_tried:
            return src.resolution_info()  # type: ignore[union-attr]
        self._resolution_tried[name] = self.clock()
        names = [n for n in self.ws.catalog.names(name, None, 5) if n != "up"]
        try:
            return await learn(names)
        except Exception as e:  # noqa: BLE001 - best effort: the resolution stays as it was
            _logger.info("resolution probe of %s failed: %r", name, e)
            return src.resolution_info()  # type: ignore[union-attr]

    async def learn_resolutions(self) -> None:
        """At daemon start: measure every live source whose resolution is not configured."""
        for name in list(self.sources):
            if getattr(self.sources.get(name), "resolution_origin", None) == "assumed":
                await self.learn_resolution(name)

    async def ensure_resolution(self, name: str) -> None:
        """Before a query: a source whose resolution is still assumed (nothing to measure when
        it was connected, e.g. a fresh VM) re-probes, at most once a minute."""
        src = self.sources.get(name)
        if getattr(src, "resolution_origin", None) != "assumed":
            return
        last = self._resolution_tried.get(name)
        if last is not None and self.clock() - last < RESOLUTION_RETRY_MS:
            return
        await self.learn_resolution(name)

    async def source_disconnect(self, name: str, actor: Actor = "claude") -> None:
        self._refuse_reserved(name)
        old = self.sources.remove(name)
        if old is not None:
            await self._close(old)
        self.log.append(actor, "source.disconnected", name, {})

    async def learn(self, source: str = "default", actor: Actor = "system") -> dict:
        """Discover a source and (re-)learn its catalog from the result, and its resolution
        from the scrape spacing of its series (bead wbw)."""
        discovery = await self._source(source).discover()
        out = self.ws.catalog_learn(source, discovery, actor)
        if (res := await self.learn_resolution(source)) is not None:
            out = {**out, "resolution": res}
        return out

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
        auto: AutoForm | None = None,
        raw_ok: bool = False,
        bounds_lo: float | None = None,
        bounds_hi: float | None = None,
        bounds_by: str | None = None,
        group: GroupRef | None = None,
    ) -> ShowResult:
        meta = self.datasets.meta(dataset_id)
        refuse_estimate(self.datasets, meta)
        # An agent-provided unit (Claude learned it from the source, the emitting
        # tool, etc.) wins over suffix inference and records who vouched for it.
        # A code output's unit is what the code declared; its expr names no catalog metric.
        provenance = f"provided by {actor}" if unit else None
        unit_warning = None
        if unit and not meta.code_node:
            unit, provenance, unit_warning = self._verify_unit(meta, unit, provenance)
        if not unit and meta.code_node and meta.unit:
            unit, provenance = meta.unit, f"declared by code node {meta.code_node}"
        spec = auto_spec(
            dataset_id,
            expr=None if meta.code_node else meta.expr,
            unit=unit,
            unit_provenance=provenance,
            representation=meta.representation,
            lookup=lambda metric: self.ws.catalog_facts(meta.source, metric),
        )
        spec.auto = auto
        spec.group = group
        if bounds_lo is not None or bounds_hi is not None:
            # the caller vouches for natural bounds, like an asserted unit (bead f2z)
            spec.y.asserted_bounds = AssertedBounds(
                lo=bounds_lo, hi=bounds_hi, by=bounds_by or actor
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
        elif mark == "spc":
            self.signal.check(dataset_id, "analyze")
            if len(windows or []) > 1:
                raise ValueError("spc takes at most one window: the baseline")
            w = (windows or [None])[0]
            if w is not None:
                resolve_baseline(meta, w.start_ms, w.end_ms)  # validates; the label is fixed
                w = Window(start_ms=w.start_ms, end_ms=w.end_ms, label="baseline")
            ref = None if w else self.diagnostics.last_reference(dataset_id)
            spec.layers = [Layer(mark="spc", data=dataset_id, windows=[w] if w else [], spc=ref)]
        elif mark == "seasonal":
            cfg = self.seasonal.last_config(dataset_id)
            spec.layers = [Layer(mark="seasonal", data=dataset_id, seasonal=cfg)]
        elif mark == "littles":
            cfg = self.littles.last_config(dataset_id)
            spec.layers = [Layer(mark="littles", data=dataset_id, littles=cfg)]
            spec.y.unit = spec.y.unit or "requests"
            panel_datasets = list(dict.fromkeys([dataset_id, *cfg["datasets"].values()]))
        elif mark == "fleet":
            cfg = self.fleets.last_config(dataset_id)
            self.fleets.panel(dataset_id, cfg)  # validates (percentiles, units, members) now
            spec.layers = [Layer(mark="fleet", data=dataset_id, fleet=cfg)]
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
        cut: list[ValidationIssue] = []
        if (
            mark in ("auto", "line+envelope")
            and len(spec.layers) == 1
            and spec.layers[0].mark == "line+envelope"
            and self.datasets.series_count(dataset_id) > LINE_SERIES_BUDGET
        ):
            cut = self._over_line_budget(dataset_id, meta, spec, mark)
        issues = validate(
            spec,
            {d: self.datasets.series_count(d) for d in panel_datasets},
            {d: self.datasets.meta(d).representation for d in panel_datasets},
        )
        issues += cut
        counters = (
            raw_counters(meta.expr, lambda m: self.ws.catalog_facts(meta.source, m))
            if spec.layers[0].mark == "line+envelope" and not meta.derived and not meta.code_node
            else []
        )
        if counters and not (auto or raw_ok):
            issues.append(
                ValidationIssue(
                    rule="raw_counter",
                    severity="warning",
                    message=(
                        f"{', '.join(counters)} is a counter, so this draws a running total that "
                        "only grows; chart rate(...[$__rate_interval]) to see what happens"
                    ),
                )
            )
        if spec.y.asserted_bounds is not None and (
            spec.signal
            or any(layer.mark not in ("line+envelope", "fleet") for layer in spec.layers)
        ):
            spec.y.asserted_bounds = None
            issues.append(
                ValidationIssue(
                    rule="bounds_not_applied",
                    severity="warning",
                    message=(
                        "bounds_lo/bounds_hi were not stored: this panel has no plain value axis "
                        "(heatmap, histogram, spc, spectrum, filter views)"
                    ),
                )
            )
        if unit_warning:
            issues.append(
                ValidationIssue(rule="unit_unverified", severity="warning", message=unit_warning)
            )
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise ChartRejected(errors)
        with self.log.transaction():
            panel = self.workspace.create_panel(question, spec.model_dump(), panel_datasets)
            self.log.append(actor, "panel.created", panel.id, {"question": panel.question})
        if (
            self.auto_profile
            and spec.layers[0].mark == "line+envelope"
            and not meta.derived
            and not meta.code_node
        ):
            self.profiles.request(meta.source, meta.expr)  # lazy T1 profile on first view
        return ShowResult(panel, [i for i in issues if i.severity == "warning"])

    def _verify_unit(
        self, meta: DatasetMeta, unit: str, provenance: str | None
    ) -> tuple[str, str | None, str | None]:
        """An asserted y unit, checked against what the catalog (or a derived-bounds rule) says
        the expression returns and against the data range (telemetry-nerd-lei). A scale or
        dimension conflict is refused; an unverifiable unit is kept, flagged in its provenance."""
        if meta.representation == "distribution":
            expected, why = None, None  # the unit labels the value axis of buckets, not a line
        else:
            derived = self._derived_bounds(meta)
            if derived is not None:
                expected, why = derived.unit, f"rule: {derived.basis}"
            elif has_division(meta.expr):
                # the token walk does not see operators: a quotient (bytes / bytes) would be
                # "inferred" in its operands' unit, so there is nothing sound to check against
                expected, why = None, None
            else:
                expected, why = infer_unit_with_provenance(
                    meta.expr, lambda m: self.ws.catalog_facts(meta.source, m)
                )
        check = check_unit(unit, expected, why, self._value_range(meta.id))
        if check.problem:
            raise ValueError(check.problem)
        if check.provenance_note:
            provenance = f"{provenance}; {check.provenance_note}"
        return unit, provenance, check.warning

    def _value_range(self, dataset_id: str) -> tuple[float, float] | None:
        """Finite (min, max) over every series and step of a series dataset, else None."""
        try:
            _, result = self.datasets.get(dataset_id)
        except (NotFound, ValueError):
            return None
        b = result.buckets
        if b is None or b.num_rows == 0 or "min" not in b.column_names:
            return None
        lo = pc.min(pc.drop_null(b["min"])).as_py() if b.num_rows else None
        hi = pc.max(pc.drop_null(b["max"])).as_py() if b.num_rows else None
        if lo is None or hi is None or not (math.isfinite(lo) and math.isfinite(hi)):
            return None
        return float(lo), float(hi)

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
        if meta.code_node:
            why = "a code output is fixed data: no operating profile, limit or last-week window"
            off = {"available": False, "reason": why}
            return {
                "flags": ov.model_dump(), "normal": off, "limit": off,
                "ghost": {**off, "loaded": False, "label": "last week"},
            }  # fmt: skip
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
                out[name] = without_data(out[name])
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

    def _over_line_budget(
        self, dataset_id: str, meta: DatasetMeta, spec: ChartSpec, mark: str
    ) -> list[ValidationIssue]:
        """More series than a line chart draws (14y): members of one group become the fleet view
        (mark auto only); otherwise the most outstanding series are lines and the rest one
        "others" band (`series_cut`). Both are said in a warning. Percentiles and declared
        intervals cannot be pooled into a band: the series_budget refusal then stands."""
        if meta.representation == "quantile" or self.datasets.interval(dataset_id) is not None:
            return []
        _, result = self.datasets.get(dataset_id)
        labels = series_labels(result.series)
        sids = list(labels)
        not_fleet = ""
        member = member_label(list(labels.values()))
        if mark == "auto" and member is not None:
            cfg = self.fleets.last_config(dataset_id)
            try:
                self.fleets.panel(dataset_id, cfg)  # validates (percentiles, units, members)
            except ValueError as e:
                not_fleet = f" (not drawn as a fleet of {member}s: {e})"
            else:
                spec.layers = [Layer(mark="fleet", data=dataset_id, fleet=cfg)]
                return [ValidationIssue(
                    rule="series_budget_fleet", severity="warning",
                    message=(
                        f"{len(sids)} series exceed the line budget ({LINE_SERIES_BUDGET}) and "
                        f"differ only by {member}: drawn as a fleet (spread band, median, "
                        "outlying members as lines; every member is in the band). Run "
                        f"fleet({dataset_id}) for the outlier tests and member counts"
                    ),
                )]  # fmt: skip
        order = rank(result.buckets, sids)
        keep, rest = order[: LINE_SERIES_BUDGET - 1], order[LINE_SERIES_BUDGET - 1 :]
        named = dict(zip(sids, names(sids, labels), strict=True))
        note = cut_note([named[s] for s in keep], [named[s] for s in rest], LINE_SERIES_BUDGET)
        spec.layers[0].top = {"keep": keep, "total": len(sids), "rank": RANK_TEXT, "note": note,
                              "others": [named[s] for s in rest]}  # fmt: skip
        return [ValidationIssue(rule="series_cut", severity="warning", message=note + not_fleet)]

    def panel_data(self, panel_id: str, width_px: int) -> dict:
        panel = self.workspace.get_panel(panel_id)
        dataset_id = panel.dataset_ids[0]
        layer0 = panel.spec["layers"][0]
        mark = layer0["mark"]
        analysis: dict | None = None
        if mark == "spectrum":
            analysis = self.signal.spectrum_panel(
                dataset_id, layer0.get("min_period_ms"), layer0.get("max_period_ms"), width_px
            )
        elif mark == "spc":
            w = (layer0.get("windows") or [None])[0]
            analysis = self.diagnostics.panel(
                dataset_id,
                w["start_ms"] if w else None,
                w["end_ms"] if w else None,
                layer0.get("spc"),
            )
        elif mark == "fleet":
            cfg = layer0.get("fleet") or {}
            analysis = self.fleets.panel(dataset_id, cfg)
            analysis["heat"] = fleet_payloads.heat(self.fleets, dataset_id, cfg)
        elif mark == "seasonal":
            analysis = self.seasonal.panel(dataset_id, layer0["seasonal"])
        elif mark == "littles":
            analysis = self.littles.panel(layer0["littles"])
        elif mark == "spectrogram":
            analysis = self.signal.spectrogram_panel(
                dataset_id, layer0["segment_ms"], layer0["overlap"], FACET_HEIGHT_SINGLE
            )
        if analysis is not None:  # an analysis panel: its own payload, no series
            return {
                "panel": panel.to_dict(),
                "dataset": self.datasets.meta(dataset_id).to_dict(),
                **analysis,
            }
        if self.datasets.meta(dataset_id).representation == "distribution":
            return self._distribution_panel_data(panel, dataset_id, width_px)
        meta, result = self.datasets.get(dataset_id)
        interval = self.datasets.interval(dataset_id)
        if meta.representation == "quantile" or interval is not None:
            # never re-aggregate percentiles over time, nor a declared interval (the interval of
            # a coarser bucket is not the union of its parts): serve at their own step
            table, effective_step = result.buckets, meta.step_ms
        else:
            table, effective_step = lod(
                result.buckets, meta.step_ms, TimeRange(meta.start_ms, meta.end_ms), width_px
            )
        labels = series_labels(result.series)
        series = series_payload(table, labels, interval)
        top = layer0.get("top")
        if top:  # over the line budget (14y): kept lines, the rest as one "others" band
            series = split(series, top["keep"], top.get("others", []))
        bundle = dataset_bundle(self.datasets, meta, result)
        caveats = self._time_summary(meta, result, self.clock(), bundle)["caveats"]
        states = bundle.companions.get("bucket_state")
        located = list(bundle.caveats)
        if meta.code_node:
            located.append(code_caveat(meta))
        if top:
            located.append(
                Caveat(code="series_cut", severity="info", message=top["note"] + ".",
                       source="validator")
            )  # fmt: skip
        state_rows: list[dict] = []
        state_more = 0
        if states is not None:
            names = {sid: series_name(lb) for sid, lb in labels.items()}
            if meta.representation != "quantile":  # presence mode has no sample counts to judge
                located += interval_caveats(states, names, meta.step_ms, meta.resolution_ms)
            if effective_step != meta.step_ms:
                states = coarsen(states, effective_step)
            failed = [tuple(f) for f in meta.failed_spans]
            located += from_bucket_state(states, names, effective_step, failed)
            state_rows, state_more = rug_payload(states)
        if meta.failed_spans and not any(c.code == "untrusted_data" for c in located):
            # failed fetches are known from the dataset even when no series carries the state
            in_window = [
                (g, r)
                for a, b, r in meta.failed_spans
                if (g := grid(max(a, meta.start_ms), min(b, meta.end_ms), meta.step_ms))
            ]
            spans = runs(sorted({t for g, _ in in_window for t in g}), meta.step_ms)
            reasons = sorted({r for _, r in in_window})  # only failures inside the window
            total = format_duration(sum(b - a for a, b in spans))
            if spans:  # a failed span wholly outside the window says nothing about it
                located.append(
                    Caveat(
                        code="untrusted_data",
                        message=f"Data unknown for {total} ({'; '.join(reasons)}).",
                        where=Where(spans=spans),
                        source="bucket_state",
                    )
                )
        if any(c.code == "missing_data" for c in located) and "gaps" in caveats:
            caveats.remove("gaps")
        for c in located:
            if c.severity != "info" and c.code not in caveats:
                caveats.append(c.code)
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
            "bucket_state": state_rows,
            "bucket_state_more": state_more,
            "located": [c.model_dump() for c in located],
            "caveats": caveats,
        }


def _semantics_flags(src) -> dict:
    """Source semantics the dataset needs when read back (only those that hold)."""
    sem = src.semantics
    return {"post_gap_increase_spike": True} if sem and sem.post_gap_increase_spike.value else {}


def _series_stats(buckets) -> list:
    """Per-series sample statistics from a dataset's buckets (the bucket mean is the sample)."""
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    return [
        scan_series(g.sort("ts_ms")["avg"].to_list())
        for _, g in df.group_by("series_id", maintain_order=True)
    ]


def _baseline_cover_ms(baseline: dict) -> int:
    """Time an analyze baseline spans: its window, or the sum of its reference windows."""
    wins = baseline.get("windows") or [baseline]
    total = 0
    for w in wins:
        try:
            start, end = w["start"], w["end"]
            total += int(
                (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
            )
        except (KeyError, TypeError, ValueError):
            continue
    return total
