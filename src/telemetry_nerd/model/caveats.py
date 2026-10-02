"""Structured caveats that know where they apply (spec 2026-10-02 §4)."""

from __future__ import annotations

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


def series_name(labels: Mapping[str, str]) -> str:
    return "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


SUBQUERY_FILLS_GAPS = "subquery_fills_gaps"
UNOBSERVABLE_MESSAGE = (
    "Coverage unknown: this expression's sample counts cannot be observed (subquery fills gaps)."
)
SPIKE_MESSAGE = (
    "value right after a gap is computed from the sample before the gap (VictoriaMetrics): "
    "increase includes the gap's growth, rate averages across it; not a real spike."
)


def _total(spans: list[tuple[int, int]]) -> str:
    return format_duration(sum(b - a for a, b in spans))


INTERVAL_RATIO = 1.5  # own scrape interval this far from the configured one is worth saying


def differing_intervals(states: pa.Table, step_ms: int, resolution_ms: int) -> dict[str, int]:
    """series id -> its own sample interval (whole seconds) for series scraped at a rate other than
    the source's configured one. Samples-mode states only (the caller gates presence mode); series
    with no samples, source-filled counts, or a step finer than the scrape are not judged."""
    if states.num_rows == 0 or resolution_ms <= 0 or step_ms < resolution_ms:
        return {}
    df = pl.from_arrow(states)
    seen = df.filter((pl.col("observed") > 0) & (pl.col("state") != int(State.UNKNOWN)))
    filled = df.filter((pl.col("flags") & int(Flag.SOURCE_FILLED)) != 0)["series_id"].unique()
    per = (
        seen.filter(~pl.col("series_id").is_in(filled.to_list()))
        .group_by("series_id")
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
    parts = [
        f"{', '.join(names.get(sid, sid) for sid in ids)}: sampled about every {secs}s"
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


def _every(step_ms: int, count: float) -> str:
    return f"{max(1, round(step_ms / count / 1000))}s"


def from_bucket_state(
    states: pa.Table, names: Mapping[str, str], step_ms: int, failed: Sequence[FailedSpan] = ()
) -> list[Caveat]:
    if states.num_rows == 0:
        return []
    df = pl.from_arrow(states)
    out: list[Caveat] = []
    unknown = df.filter(pl.col("state") == int(State.UNKNOWN))
    if unknown.height:
        affected = sorted(unknown["series_id"].unique().to_list())
        everyone = len(affected) == df["series_id"].n_unique()
        spans = runs(unknown["ts_ms"].unique().to_list(), step_ms)
        reasons = sorted({r for *_ab, r in failed}) or ["source could not tell"]
        message = f"Data unknown for {_total(spans)} ({'; '.join(reasons)})."
        if ((unknown["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():
            # the expression itself hides coverage: one dataset-level reason, not per bucket
            message = UNOBSERVABLE_MESSAGE
            if failed:
                message += f" Also failed fetches ({'; '.join(reasons)})."
        out.append(
            Caveat(
                code="untrusted_data",
                message=message,
                where=Where(spans=spans, series=None if everyone else affected),
                source="bucket_state",
            )
        )
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
                    message=f"{name}: first seen {_total(absent)} after the window starts.",
                    where=Where(spans=absent, series=[sid]),
                    source="bucket_state",
                )
            )
    return out


def _interval_change_caveat(
    sid: str, name: str, g: pl.DataFrame, changed: pl.DataFrame, step_ms: int
) -> Caveat:
    spans = runs(changed["ts_ms"].to_list(), step_ms)
    here = changed.filter(pl.col("observed") > 0)["observed"].median()
    other = g.filter(
        ((pl.col("flags") & int(Flag.INTERVAL_CHANGE)) == 0) & (pl.col("observed") > 0)
    )["observed"].median()
    early = changed["ts_ms"].min() < g.filter(pl.col("observed") > 0)["ts_ms"].median()
    rates = (here, other) if early else (other, here)
    around = spans[-1][1] if early else spans[0][0]
    a, b = (_every(step_ms, r) for r in rates)
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
