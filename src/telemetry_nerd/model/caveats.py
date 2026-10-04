"""Structured caveats that know where they apply (spec 2026-10-02 §4)."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from typing import Literal

import polars as pl
import pyarrow as pa
from pydantic import BaseModel

from telemetry_nerd.model.bucket_state import FailedSpan, Flag, State
from telemetry_nerd.model.time import format_duration, iso

Severity = Literal["info", "warn", "blocks_claim"]


class Where(BaseModel):
    spans: list[tuple[int, int]] | None = None  # real time [start, end] in ms
    series: list[str] | None = None  # series ids; None = all series


class Caveat(BaseModel):
    code: str
    severity: Severity = "warn"
    message: str
    where: Where | None = None  # None = whole panel
    source: str


def runs(ts: Sequence[int], step_ms: int) -> list[tuple[int, int]]:
    """Contiguous bucket ends -> time spans; a bucket ending at t covers (t - step, t]."""
    out: list[tuple[int, int]] = []
    for t in sorted(ts):
        if out and t - out[-1][1] == step_ms:
            out[-1] = (out[-1][0], t)
        else:
            out.append((t - step_ms, t))
    return out


NO_REASON = "source could not tell"
MAX_WHERE_SERIES = 50  # located caveats name at most this many series


def failure_reasons(failed: Sequence[FailedSpan], ts: Sequence[int], step_ms: int) -> list[str]:
    """Reasons of the failed fetches that touch these buckets (ends `ts`, each covering
    (t - step, t]): a bucket is in a failed span [a, b] when a <= t < b + step. Failed spans are
    grid-aligned (a, b are bucket ends of the dataset's step), so at that step this is exactly
    compute's a <= t <= b; for coarser buckets it is deliberately wider (a failed fine bucket
    anywhere inside the coarse one counts), and an off-grid span reaches the bucket it overlaps. Sorted, unique;
    empty when no failure reaches them (a caveat never cites a failure elsewhere in the window)."""
    ordered = sorted(ts)
    out: set[str] = set()
    for a, b, reason in failed:
        i = bisect_left(ordered, a)
        if i < len(ordered) and ordered[i] < b + step_ms:
            out.add(reason)
    return sorted(out)


def series_name(labels: Mapping[str, str]) -> str:
    return "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


SUBQUERY_FILLS_GAPS = "subquery_fills_gaps"
CADENCE_OR_LOSS = "cadence_or_loss"
CADENCE_REASON = (
    "0-sample buckets at a regular spacing: a series interval a little slower than the query "
    "step, or a scrape lost at that spacing; counts cannot tell"
)
UNOBSERVABLE_MESSAGE = (
    "Coverage unknown: this expression's sample counts cannot be observed (subquery fills gaps)."
)
SPIKE_MESSAGE = (
    "value right after a gap is computed from the sample before the gap (VictoriaMetrics): "
    "increase/delta include the whole gap's change, idelta returns the raw sample; "
    "not a real spike."
)


def _total(spans: list[tuple[int, int]]) -> str:
    return format_duration(sum(b - a for a, b in spans))


INTERVAL_RATIO = 1.5  # own scrape interval this far from the configured one is worth saying


def differing_intervals(states: pa.Table, step_ms: int, resolution_ms: int) -> dict[str, int]:
    """series id -> its own sample interval (whole seconds) for series scraped at a rate other than
    the source's configured one: the dominant one, the median over its ok/partial buckets of the
    local interval (step / expected). Samples-mode states only (the caller gates presence mode);
    series with no samples, source-filled counts, or a step finer than the scrape are not
    judged."""
    if states.num_rows == 0 or resolution_ms <= 0 or step_ms < resolution_ms:
        return {}
    df = pl.from_arrow(states)
    sampled = df.filter(pl.col("observed") > 0)["series_id"].unique()
    filled = df.filter((pl.col("flags") & int(Flag.SOURCE_FILLED)) != 0)["series_id"].unique()
    seen = df.filter(
        pl.col("state").is_in([int(State.OK), int(State.PARTIAL)])
        & pl.col("series_id").is_in(sampled.to_list())
        & ~pl.col("series_id").is_in(filled.to_list())
    )
    per = (
        seen.group_by("series_id")
        .agg(pl.col("expected").median().alias("expected"))
        .sort("series_id")
    )
    out: dict[str, int] = {}
    for sid, expected in per.iter_rows():
        ratio = step_ms / expected / resolution_ms
        if ratio >= INTERVAL_RATIO or ratio <= 1 / INTERVAL_RATIO:
            out[sid] = max(1, round(step_ms / expected / 1000))
    return out


def interval_caveats(
    states: pa.Table, names: Mapping[str, str], step_ms: int, resolution_ms: int
) -> list[Caveat]:
    """One dataset-level info caveat: series sampled at a rate other than the configured one are
    judged against their own rate, so loss lasting most of the window cannot show."""
    diff = differing_intervals(states, step_ms, resolution_ms)
    if not diff:
        return []
    groups: dict[int, list[str]] = {}
    for sid, secs in diff.items():
        groups.setdefault(secs, []).append(sid)
    step_s = max(1, round(step_ms / 1000))
    parts = [
        f"{', '.join(names.get(sid, sid) for sid in ids)}: sampled "
        # one sample per bucket is a floor: the series may be scraped even slower than the step
        f"{'at least' if secs == step_s else 'about'} every {secs}s"
        for secs, ids in sorted(groups.items())
    ]
    message = (
        f"{'; '.join(parts)} (source configured {format_duration(resolution_ms)}); coverage is "
        "judged against each series' own rate, so loss lasting most of the window cannot show."
    )
    return [
        Caveat(
            code="interval_differs",
            severity="info",
            message=message,
            where=Where(series=list(diff)),
            source="bucket_state",
        )
    ]


def _every(step_ms: int, buckets: pl.DataFrame) -> str:
    """Time per sample over ok/partial buckets (a slower-than-step series' ok 0 buckets count)."""
    seen = buckets.filter(pl.col("state").is_in([int(State.OK), int(State.PARTIAL)]))
    samples = seen["observed"].sum()
    return f"{max(1, round(seen.height * step_ms / samples / 1000))}s" if samples else "?"


def from_bucket_state(
    states: pa.Table, names: Mapping[str, str], step_ms: int, failed: Sequence[FailedSpan] = ()
) -> list[Caveat]:
    if states.num_rows == 0:
        return []
    df = pl.from_arrow(states)
    out = _untrusted(df, step_ms, failed)
    for (sid,), g in df.sort("ts_ms").group_by("series_id", maintain_order=True):
        name = names.get(sid, sid)
        empty_ts = g.filter(pl.col("state") == int(State.EMPTY))["ts_ms"].to_list()
        partial_ts = g.filter(pl.col("state") == int(State.PARTIAL))["ts_ms"].to_list()
        empty, partial = runs(empty_ts, step_ms), runs(partial_ts, step_ms)
        if empty or partial:
            parts = [f"no samples for {_total(empty)}"] if empty else []
            parts += [f"fewer samples than expected for {_total(partial)}"] if partial else []
            out.append(
                Caveat(
                    code="missing_data",
                    message=f"{name}: {', '.join(parts)}.",
                    # one highlight per contiguous stretch of trouble, whatever its kind
                    where=Where(spans=runs(empty_ts + partial_ts, step_ms), series=[sid]),
                    source="bucket_state",
                )
            )
        spike = runs(
            g.filter(
                (pl.col("state") != int(State.UNKNOWN))
                & ((pl.col("flags") & int(Flag.POST_GAP)) != 0)
            )["ts_ms"].to_list(),
            step_ms,
        )
        if spike:
            out.append(
                Caveat(
                    code="post_gap_spike",
                    message=f"{name}: {SPIKE_MESSAGE}",
                    where=Where(spans=spike, series=[sid]),
                    source="bucket_state",
                )
            )
        changed = g.filter((pl.col("flags") & int(Flag.INTERVAL_CHANGE)) != 0)
        if changed.height:
            out.append(_interval_change_caveat(sid, name, g, changed, step_ms))
        absent = runs(g.filter(pl.col("state") == int(State.ABSENT))["ts_ms"].to_list(), step_ms)
        if absent:
            out.append(
                Caveat(
                    code="absent_part",
                    severity="info",
                    message=f"{name}: first seen {_total(absent)} after the window starts "
                    "(membership change is normal lifecycle, not a fault; n moves).",
                    where=Where(spans=absent, series=[sid]),
                    source="bucket_state",
                )
            )
    return out


def _untrusted(df: pl.DataFrame, step_ms: int, failed: Sequence[FailedSpan]) -> list[Caveat]:
    """One `untrusted_data` caveat per set of series that share their unknown spans (a failed fetch
    hits every series alike, so normally one). Spans stay per series: a series is not marked
    unknown where only another one was, and each caveat cites only the failures that touch its own
    spans."""
    unknown = df.filter(pl.col("state") == int(State.UNKNOWN))
    if not unknown.height:
        return []
    n_series = df["series_id"].n_unique()
    groups: dict[tuple[tuple[int, int], ...], list[str]] = {}
    for (sid,), g in unknown.sort("series_id").group_by("series_id", maintain_order=True):
        groups.setdefault(tuple(runs(g["ts_ms"].to_list(), step_ms)), []).append(sid)
    out: list[Caveat] = []
    for spans, sids in groups.items():
        rows = unknown.filter(pl.col("series_id").is_in(sids))
        reasons = failure_reasons(failed, rows["ts_ms"].unique().to_list(), step_ms)
        if ((rows["flags"] & int(Flag.CADENCE)) != 0).any():
            reasons.append(CADENCE_REASON)
        message = f"Data unknown for {_total(list(spans))} ({'; '.join(reasons or [NO_REASON])})."
        if ((rows["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():
            # the expression itself hides coverage: one dataset-level reason, not per bucket
            message = UNOBSERVABLE_MESSAGE
            if reasons:
                message += f" Also failed fetches ({'; '.join(reasons)})."
        if len(sids) > MAX_WHERE_SERIES and len(sids) != n_series:
            message += f" Affects {len(sids)} series (the first {MAX_WHERE_SERIES} are named)."
        out.append(
            Caveat(
                code="untrusted_data",
                message=message,
                where=Where(
                    spans=list(spans),
                    series=None if len(sids) == n_series else sids[:MAX_WHERE_SERIES],
                ),
                source="bucket_state",
            )
        )
    return out


def _interval_change_caveat(
    sid: str, name: str, g: pl.DataFrame, changed: pl.DataFrame, step_ms: int
) -> Caveat:
    spans = runs(changed["ts_ms"].to_list(), step_ms)
    other = g.filter((pl.col("flags") & int(Flag.INTERVAL_CHANGE)) == 0)
    other_at = other.filter(pl.col("observed") > 0)["ts_ms"].median()
    early = other_at is not None and changed["ts_ms"].median() < other_at
    # the first sample closes an interval that began before the window: not part of either rate
    f0 = g.filter(pl.col("observed") > 0)["ts_ms"].min()
    parts = [
        p.filter(pl.col("ts_ms") != f0) for p in ((changed, other) if early else (other, changed))
    ]
    around = spans[-1][1] if early else spans[0][0]
    a, b = (_every(step_ms, p) for p in parts)
    return Caveat(
        code="interval_change",
        severity="info",
        message=(
            f"{name}: sample rate changed within the window (about every {a} \u2192 {b} around "
            f"{iso(around)}); buckets at the other rate may read ok or partial."
        ),
        where=Where(spans=spans, series=[sid]),
        source="bucket_state",
    )
