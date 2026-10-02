"""Structured caveats that know where they apply (spec 2026-10-02 §4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import polars as pl
import pyarrow as pa
from pydantic import BaseModel

from telemetry_nerd.model.bucket_state import FailedSpan, State
from telemetry_nerd.model.time import format_duration

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


def _total(spans: list[tuple[int, int]]) -> str:
    return format_duration(sum(b - a for a, b in spans))


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
        out.append(
            Caveat(
                code="untrusted_data",
                message=f"Data unknown for {_total(spans)} ({'; '.join(reasons)}).",
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
