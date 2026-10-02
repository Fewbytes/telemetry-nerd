"""T1 operating profiles: lazy, cached, refreshed daily (spec §4.2; bead 2as.7).

Statistics live in `analysis.profile`; this module decides WHAT is profiled (`profile_target`),
WHERE the data comes from (the source's paired profile source), and WHEN to (re)compute.
Design notes: docs/superpowers/specs/2026-10-01-operating-profile-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.exprkind import (
    RATE_INTERVAL,
    analyze,
    looks_like_histogram,
    rate_interval_ms,
)
from telemetry_nerd.analysis.profile import (
    DAY_HOURS,
    HOUR_MS,
    WEEK_HOURS,
    Kind,
    ProfileStats,
    Seasonal,
    compute_profile,
    seasonal_profile,
)
from telemetry_nerd.catalog.profiles import ProfileStore, profile_id
from telemetry_nerd.catalog.rules import Facts
from telemetry_nerd.charts.units import raw_counters
from telemetry_nerd.core.workspace_service import WorkspaceService
from telemetry_nerd.datasets.cache import SeriesCache
from telemetry_nerd.datasets.store import is_code_expr
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import TimeRange, format_duration, now_ms, parse_duration
from telemetry_nerd.sources.base import Source, SourceError, SourceUnavailable
from telemetry_nerd.sources.promql import is_selector
from telemetry_nerd.sources.registry import SourceRegistry

log = logging.getLogger(__name__)

PROFILE_STEP_MS = HOUR_MS
PROFILE_WINDOW_MS = 30 * 24 * HOUR_MS
PROFILE_TTL_MS = 24 * HOUR_MS  # refreshed daily
FAILURE_RETRY_MS = HOUR_MS  # a failed source is not re-queried on every view

_METRIC = re.compile(r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{|$)")
_BARE_NAME = re.compile(r"^\s*[a-zA-Z_:][a-zA-Z0-9_:]*\s*$")
#: rate-like calls over a plain selector: their window only sets the averaging, so it can be
#: widened to the profile step without changing what the value means
_RATE_CALL = re.compile(r"\b(rate|irate|deriv)\s*\(\s*([^()\[\]]*?)\s*\[\s*([0-9a-z]+)\s*\]\s*\)")


class ProfileRefused(SourceError):
    """The expression has no meaningful operating profile as written."""


class ProfileUnavailable(SourceError):
    """Computing the profile failed (recently); the reason and when to retry are in the hint."""


@dataclass(frozen=True)
class ProfileTarget:
    expr: str  # what is profiled at the profile step
    kind: Kind
    rate_window_ms: int | None


def profile_target(
    expr: str,
    lookup: Callable[[str], Facts],
    resolution_ms: int,
    step_ms: int = PROFILE_STEP_MS,
) -> ProfileTarget:
    """What to profile for an expression a user is looking at.

    Counters are profiled as rates, rate windows are widened to the profile step's
    $__rate_interval, quantiles stay per-step quantiles. Raises ProfileRefused."""
    window = rate_interval_ms(step_ms, resolution_ms)
    w = format_duration(window)
    e = expr.strip().replace(RATE_INTERVAL, w)
    if is_selector(e):
        m = _METRIC.match(e)
        facts = lookup(m.group(1)) if m else None
        if looks_like_histogram(e) or (facts is not None and facts.type == "histogram"):
            raise ProfileRefused(
                "a histogram has no single operating range",
                hint=(
                    "profile a quantile, histogram_quantile(0.99, sum by (le) "
                    "(rate(x_bucket[$__rate_interval]))), or the request rate rate(x_count[...])"
                ),
            )
        if facts is not None and facts.type == "counter":
            return ProfileTarget(f"rate({e}[{w}])", "rate", window)
        return ProfileTarget(e, "level", None)
    if raw_counters(e, lookup):
        raise ProfileRefused(
            "the expression uses a counter's running total, which only grows",
            hint="wrap the counter in rate(...[$__rate_interval]) and profile that",
        )
    if looks_like_histogram(e):
        raise ProfileRefused(
            "histogram buckets drawn as lines have no single operating range",
            hint="profile histogram_quantile(q, sum by (le) (rate(x_bucket[$__rate_interval])))",
        )

    def widen(mt: re.Match) -> str:
        try:
            current = parse_duration(mt.group(3))
        except ValueError:
            return mt.group(0)
        if current >= window:
            return mt.group(0)
        return f"{mt.group(1)}({mt.group(2)}[{w}])"

    e = _RATE_CALL.sub(widen, e)
    windows = []
    for mt in _RATE_CALL.finditer(e):
        try:
            windows.append(parse_duration(mt.group(3)))
        except ValueError:
            pass
    rate_window = max(windows) if windows else None
    if analyze(e).quantile is not None:
        return ProfileTarget(e, "quantile", rate_window)
    if windows:
        return ProfileTarget(e, "rate", rate_window)
    return ProfileTarget(e, "level", None)


class OperatingProfile(ProfileStats):
    id: str
    source: str  # the source the user looks at
    profiled_from: str  # the source actually queried (the paired profile source if live)
    expr: str  # profiled expression
    rate_window_ms: int | None
    window_ms: int
    computed_at_ms: int
    #: computed on read: older than the refresh interval (a refresh is due or failing)
    stale: bool = False

    def summary(self, max_series: int = 12) -> dict:
        """Compact form for Claude: no bucket arrays."""
        out = {
            "id": self.id,
            "expr": self.expr,
            "kind": self.kind,
            "source": self.source,
            "profiled_from": self.profiled_from,
            "window": format_duration(self.window_ms),
            "timezone": self.tz,
            "step": format_duration(self.step_ms),
            "rate_window": format_duration(self.rate_window_ms) if self.rate_window_ms else None,
            "computed_at_ms": self.computed_at_ms,
            "stale": self.stale,
            "pooled": self.pooled.model_dump(),
            "series_total": self.series_total,
            "caveats": self.caveats,
            "series": [],
        }
        for s in self.series[:max_series]:
            row: dict = {
                "labels": s.labels,
                "n": s.n,
                "coverage": round(s.coverage, 3),
                "history": format_duration(s.history_ms) if s.history_ms else None,
                "range": s.range.model_dump(exclude={"n"}),
                "caveats": s.caveats,
            }
            if s.seasonal is not None:
                full = [b for b in s.seasonal.buckets if b.level is not None]
                peak = max(full, key=lambda b: b.level or 0.0)
                trough = min(full, key=lambda b: b.level or 0.0)
                widths = sorted((b.hi or 0.0) - (b.lo or 0.0) for b in full)
                row["seasonal"] = {
                    "period": s.seasonal.period,
                    "tz": s.seasonal.tz,
                    "amplitude": s.seasonal.amplitude,
                    "peak_bucket": peak.i,
                    "trough_bucket": trough.i,
                    "median_band_width": widths[len(widths) // 2],
                    "band_coverage": s.seasonal.band_coverage,
                    "scores": s.seasonal.scores,
                    "sparse_buckets": s.seasonal.sparse_buckets,
                }
            out["series"].append(row)
        return out


@dataclass(frozen=True)
class SeasonalShapes:
    """Per-series seasonal profiles re-estimated from the profile's hourly history with an
    excluded span (the data about to be judged) removed: never judge data with a centre
    computed from it."""

    profile_id: str
    computed_at_ms: int
    expr: str
    history_ms: int
    excluded_hours: int
    series: list[tuple[dict, Seasonal]]  # (labels, seasonal), period != none

    def for_labels(self, labels: dict) -> Seasonal | None:
        for lb, s in self.series:
            if lb == labels:
                return s
        return self.series[0][1] if len(self.series) == 1 else None

    @staticmethod
    def cycle_s(s: Seasonal) -> float:
        return {"hour_of_day": DAY_HOURS, "hour_of_week": WEEK_HOURS}.get(s.period, 0) * 3600.0


@dataclass
class ProfileService:
    sources: SourceRegistry
    cache: SeriesCache
    store: ProfileStore
    ws: WorkspaceService
    clock: Callable[[], int] = now_ms
    window_ms: int = PROFILE_WINDOW_MS
    step_ms: int = PROFILE_STEP_MS
    ttl_ms: int = PROFILE_TTL_MS
    retry_ms: int = FAILURE_RETRY_MS
    _inflight: dict[tuple[str, str], asyncio.Future] = field(default_factory=dict)
    _background: set[asyncio.Future] = field(default_factory=set)  # keep tasks referenced
    _shapes: dict[tuple, SeasonalShapes | None] = field(default_factory=dict)

    # resolution ---------------------------------------------------------
    def _profile_source(self, name: str) -> tuple[Source, list[str]]:
        src = self.sources.get(name)
        if src is None:
            raise SourceError(f"unknown source {name!r}", hint="see source_list for names")
        spec = self.sources.spec(name)
        paired = spec.profile_source if spec is not None else None
        if paired is None:
            return src, []
        psrc = self.sources.get(paired)
        if psrc is None:
            return src, ["profile_source_unavailable"]
        return psrc, []

    def target(self, source: str, expr: str) -> tuple[ProfileTarget, Source, list[str]]:
        if is_code_expr(expr):
            raise SourceError(
                f"{expr.strip()} is a code output (fixed data): it has no operating profile",
                hint="profile the code's input expression instead",
            )
        psrc, caveats = self._profile_source(source)
        t = profile_target(
            expr, lambda m: self.ws.catalog_facts(source, m), psrc.resolution_ms, self.step_ms
        )
        return t, psrc, caveats

    # reads --------------------------------------------------------------
    def _load(self, source: str, expr: str) -> OperatingProfile | None:
        row = self.store.get(source, expr)
        if row is None or row.data is None:
            return None
        p = OperatingProfile.model_validate_json(row.data)
        # a profile counted in another timezone than the source's now is wrong, whatever its age
        p.stale = self.clock() - p.computed_at_ms >= self.ttl_ms or p.tz != self._tz(source)
        return p

    def _tz(self, source: str) -> str:
        spec = self.sources.spec(source)
        return spec.timezone if spec is not None else "UTC"

    def cached(self, source: str, expr: str) -> OperatingProfile | None:
        """The stored profile (maybe stale), never computing. None when there is none yet or
        the expression cannot be profiled."""
        try:
            t, _, _ = self.target(source, expr)
        except SourceError:
            return None
        return self._load(source, t.expr)

    def by_id(self, pid: str) -> OperatingProfile | None:
        row = self.store.by_id(pid)
        if row is None or row.data is None:
            return None
        p = OperatingProfile.model_validate_json(row.data)
        p.stale = self.clock() - p.computed_at_ms >= self.ttl_ms
        return p

    def hourly(self, p: OperatingProfile) -> tuple[pl.DataFrame, dict[str, dict], pa.Table] | None:
        """The profile's hourly history from the series cache (never fetches): finite values only,
        {series_id: labels}, and the series table. None when the history is not cached."""
        psrc = self.sources.get(p.profiled_from)
        if psrc is None:
            return None
        qexpr = p.expr if is_selector(p.expr) else f"values|{p.expr}"
        got = self.cache.peek(psrc.identity, qexpr, TimeRange(p.start_ms, p.end_ms), p.step_ms)
        if got.failed:  # a partial chunk is a gappy history: like a failed fetch, not "cached"
            return None
        df = pl.from_arrow(got.buckets)
        assert isinstance(df, pl.DataFrame)
        if df.height == 0:
            return None
        df = df.filter(pl.col("count") > 0).with_columns(pl.col("avg").fill_nan(None))
        df = df.drop_nulls("avg")
        labels = {
            sid: json.loads(lab)
            for sid, lab in zip(
                got.series.column("series_id").to_pylist(),
                got.series.column("labels").to_pylist(),
                strict=True,
            )
        }
        return df, labels, got.series

    def seasonal_excluding(
        self, source: str, expr: str, start_ms: int, end_ms: int
    ) -> SeasonalShapes | None:
        """The cached profile's seasonal models re-fitted without the hours that overlap
        [start_ms, end_ms]. Sync: reads the profile's hourly history from the series cache,
        never fetches. None when there is no seasonal profile or its history is not cached."""
        p = self.cached(source, expr)
        if p is None or not any(s.seasonal and s.seasonal.period != "none" for s in p.series):
            return None
        key = (p.id, p.computed_at_ms, start_ms // HOUR_MS, end_ms // HOUR_MS)
        if key in self._shapes:
            return self._shapes[key]
        hourly = self.hourly(p)
        if hourly is None:
            return None
        df, labels, _ = hourly
        # bucket ts = END of its hour: the hour (ts - step, ts] overlaps (start, end]
        keep = (pl.col("ts_ms") <= start_ms) | (pl.col("ts_ms") - p.step_ms >= end_ms)
        excluded = df.filter(~keep)["ts_ms"].n_unique()
        df = df.filter(keep)
        wanted = {s.series_id for s in p.series if s.seasonal and s.seasonal.period != "none"}
        out = []
        for (sid,), g in df.sort("ts_ms").group_by("series_id", maintain_order=True):
            if sid not in wanted:
                continue
            ts = g["ts_ms"].to_numpy()
            seas = seasonal_profile(ts - p.step_ms, g["avg"].to_numpy(), tz=p.tz)
            if seas is not None and seas.period != "none":
                out.append((labels.get(sid, {}), seas))
        res = (
            SeasonalShapes(
                p.id, p.computed_at_ms, p.expr, p.end_ms - p.start_ms + p.step_ms, excluded, out
            )
            if out
            else None
        )
        self._shapes[key] = res
        if len(self._shapes) > 32:
            self._shapes.pop(next(iter(self._shapes)))
        return res

    # compute ------------------------------------------------------------
    async def ensure(self, source: str, expr: str, *, force: bool = False) -> OperatingProfile:
        """The profile, computed now if missing, older than a day, or forced.

        Concurrent callers share one computation. A failure is remembered for `retry_ms`;
        until then the last good profile (stale) is returned, or ProfileUnavailable raised."""
        t, psrc, caveats = self.target(source, expr)
        key = (source, t.expr)
        have = self._load(source, t.expr)
        if not force:
            if have is not None and not have.stale:
                return have
            row = self.store.get(source, t.expr)
            if (
                row is not None
                and row.failed_at_ms is not None
                and self.clock() - row.failed_at_ms < self.retry_ms
            ):
                if have is not None:
                    return have
                raise ProfileUnavailable(
                    f"profiling failed recently: {row.error}",
                    hint=f"retried automatically after {format_duration(self.retry_ms)}; "
                    "or pass refresh=true",
                )
        fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.ensure_future(self._compute(source, t, psrc, caveats))
            self._inflight[key] = fut
            fut.add_done_callback(lambda _f: self._inflight.pop(key, None))
        return await asyncio.shield(fut)

    def request(self, source: str, expr: str) -> OperatingProfile | None:
        """On view: return what is cached and, if it is missing or stale, compute it in the
        background (when an event loop is running). Never raises."""
        try:
            t, _, _ = self.target(source, expr)
        except (SourceError, ValueError):
            return None
        have = self._load(source, t.expr)
        if have is not None and not have.stale:
            return have
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return have
        if (source, t.expr) not in self._inflight:
            task = asyncio.ensure_future(self.ensure(source, expr))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            task.add_done_callback(_swallow)
        return have

    async def _compute(
        self, source: str, t: ProfileTarget, psrc: Source, caveats: list[str]
    ) -> OperatingProfile:
        now = self.clock()
        step = self.step_ms
        end = now // step * step  # end of the last complete hour
        rng = TimeRange(end - self.window_ms + step, end)
        try:
            if is_selector(t.expr):
                extremes = True
                result = await self.cache.get(
                    psrc.identity, t.expr, rng, step, lambda r: psrc.fetch(t.expr, r, step)
                )
            else:
                extremes = False
                result = await self.cache.get(
                    psrc.identity,
                    f"values|{t.expr}",
                    rng,
                    step,
                    lambda r: psrc.fetch_values(t.expr, r, step),
                )
            if result.failed:  # a profile over a gappy window would mislead; keep the stale one
                raise SourceUnavailable(result.failed[0][2], hint="retry shortly")
        except SourceError as e:
            with self.ws.log.transaction():
                self.store.put_failed(source, t.expr, now, str(e))
                self.ws.log.append(
                    "system",
                    "profile.failed",
                    profile_id(source, t.expr),
                    {"source": source, "expr": t.expr, "error": str(e)},
                )
            raise ProfileUnavailable(f"profiling failed: {e}", hint=e.hint) from e
        stats = compute_profile(
            result.buckets,
            result.series,
            start_ms=rng.start_ms,
            end_ms=rng.end_ms,
            step_ms=step,
            extremes=extremes,
            kind=t.kind,
            tz=self._tz(source),
        )
        if result.partial:
            caveats = [*caveats, "partial"]
        profile = OperatingProfile(
            **{**stats.model_dump(), "caveats": [*caveats, *stats.caveats]},
            id=profile_id(source, t.expr),
            source=source,
            profiled_from=psrc.name,
            expr=t.expr,
            rate_window_ms=t.rate_window_ms,
            window_ms=self.window_ms,
            computed_at_ms=now,
        )
        with self.ws.log.transaction():
            self.store.put_ok(source, t.expr, now, profile.model_dump_json(exclude={"stale"}))
            self.ws.log.append(
                "system",
                "profile.computed",
                profile.id,
                {
                    "source": source,
                    "expr": t.expr,
                    "series": profile.series_total,
                    "caveats": profile.caveats,
                },
            )
        self._link_catalog(source, profile)
        return profile

    def _link_catalog(self, source: str, profile: OperatingProfile) -> None:
        """A catalogued metric viewed by bare name points at its profile (2as.12 metric card)."""
        metric = _bare_metric(profile)
        if metric is None:
            return
        try:
            entry = self.ws.catalog_entry(source, metric)
        except NotFound:
            return
        mine = next(
            (c for c in entry.claims.get("operating_profile_ref", []) if c.origin == "stats"), None
        )
        if mine is not None and mine.value == profile.id:
            return
        self.ws.catalog_claim(
            source,
            metric,
            "operating_profile_ref",
            profile.id,
            "stats",
            "system",
            confidence=1.0,
            citation=f"T1 profile of {profile.expr} over {format_duration(profile.window_ms)}",
        )


def _bare_metric(profile: OperatingProfile) -> str | None:
    e = profile.expr
    if _BARE_NAME.match(e):
        return e.strip()
    m = re.match(r"^rate\(\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\[[0-9a-z]+\]\)$", e)
    return m.group(1) if m and profile.kind == "rate" else None


def _swallow(task: asyncio.Future) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.info("background operating profile failed: %s", exc)
