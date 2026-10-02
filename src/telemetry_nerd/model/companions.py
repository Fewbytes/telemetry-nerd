"""Companion series and how ops carry them (spec 2026-10-02 §3)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.exprkind import _close, _mask_strings, _strip_comments
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, coarsen, compute
from telemetry_nerd.model.caveats import Caveat, from_bucket_state
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import parse_duration
from telemetry_nerd.sources.observed import counts_are_observed

Policy = Literal["carry", "recompute", "derive", "drop"]

SOURCE_AGGREGATED = re.compile(
    r"\b(sum|avg|min|max|count|group|stddev|stdvar|topk|bottomk|quantile|count_values)\b\s*"
    r"(by|without)?\s*(\([^)]*\))?\s*\(",
    re.IGNORECASE,
)


# increase/rate use the sample before the window on VictoriaMetrics (VQ2); verified for these two
_PREVIOUS_SAMPLE_FUNCS = re.compile(r"\b(?:increase|rate)\s*\(", re.IGNORECASE)


_RANGE = re.compile(r"\[\s*([0-9]+[a-z]+)\s*(?::[^\]]*)?\]")


def previous_sample_windows(expr: str) -> list[int | None]:
    """Range (ms) of each increase()/rate() call in `expr`, None where it cannot be parsed;
    empty when the expression has no such call."""
    text = _mask_strings(_strip_comments(expr))
    out: list[int | None] = []
    for call in _PREVIOUS_SAMPLE_FUNCS.finditer(text):
        try:
            args = text[call.end() : _close(text, call.end() - 1)]
        except ValueError:
            out.append(None)
            continue
        m = _RANGE.search(args)
        try:
            out.append(parse_duration(m.group(1)) if m else None)
        except ValueError:
            out.append(None)
    return out


def post_gap_buckets(expr: str, step_ms: int) -> int:
    """How many buckets after a gap carry the gap: a window of w reaches back over the gap for
    ceil(w / step) steps; the first bucket only if the range cannot be parsed. 0: not applicable."""
    windows = previous_sample_windows(expr)
    if not windows:
        return 0
    return max([1, *(-(-w // step_ms) for w in windows if w)])


@dataclass(frozen=True)
class CompanionKind:
    name: str
    schema: pa.Schema
    coarsen: Callable[[pa.Table, int], pa.Table]
    caveats: Callable[..., list[Caveat]]


KINDS: dict[str, CompanionKind] = {
    "bucket_state": CompanionKind("bucket_state", STATE_SCHEMA, coarsen, from_bucket_state),
}

OPS: dict[str, dict[str, Policy]] = {
    "query": {"bucket_state": "derive"},
    "query_distribution": {"bucket_state": "derive"},
    "lod": {"bucket_state": "recompute"},
    "dist_rebucket": {"bucket_state": "recompute"},
    # filters change values, not which buckets were observed
    "lowpass": {"bucket_state": "carry"},
    "highpass": {"bucket_state": "carry"},
    "bandpass": {"bucket_state": "carry"},
}


def policy(op: str, kind: str) -> Policy:
    return OPS.get(op, {}).get(kind, "drop")


@dataclass(frozen=True)
class Bundle:
    primary: pa.Table
    series: pa.Table
    companions: dict[str, pa.Table] = field(default_factory=dict)
    caveats: list[Caveat] = field(default_factory=list)


def derive_states(meta, result: FetchResult) -> pa.Table:
    """bucket_state of a stored dataset: pure function of its buckets, failed spans, expression
    and the semantics hints recorded at query time."""
    mode = "presence" if meta.representation == "quantile" else "samples"
    failed = [tuple(f) for f in getattr(meta, "failed_spans", [])]
    return compute(
        result.buckets,
        result.series["series_id"].to_pylist(),
        start_ms=meta.start_ms,
        end_ms=meta.end_ms,
        step_ms=meta.step_ms,
        resolution_ms=meta.resolution_ms,
        mode=mode,
        failed=failed,
        # counts that are subquery evaluations (lookback-filled) cannot show coverage
        source_filled=not counts_are_observed(meta.expr),
        post_gap_buckets=(
            post_gap_buckets(meta.expr, meta.step_ms)
            if getattr(meta, "semantics_flags", {}).get("post_gap_increase_spike")
            else 0
        ),
    )


def dataset_bundle(store, meta, result: FetchResult) -> Bundle:
    op = meta.derived["op"] if meta.derived else "query"
    caveats: list[Caveat] = []
    companions: dict[str, pa.Table] = {}
    p = policy(op, "bucket_state")
    dropped = False
    if p == "derive":
        companions["bucket_state"] = derive_states(meta, result)
    elif p == "carry":
        src_meta, src_result = store.get(meta.derived["from"])
        src = dataset_bundle(store, src_meta, src_result).companions.get("bucket_state")
        if src is None:
            dropped = True
        else:
            ids = result.series["series_id"].to_pylist()
            companions["bucket_state"] = (
                pl.from_arrow(src)
                .filter(pl.col("series_id").is_in(ids))
                .to_arrow()
                .cast(STATE_SCHEMA)
            )
    elif p == "recompute":
        raise ValueError(
            f"{op} recomputes bucket_state at render time (coarsen), not on stored datasets"
        )
    else:
        dropped = True
    if dropped:
        caveats.append(
            Caveat(
                code="companion_dropped",
                severity="info",
                message=f"Coverage is not tracked through {op}.",
                source=f"op:{op}",
            )
        )
    if SOURCE_AGGREGATED.search(meta.expr):
        caveats.append(
            Caveat(
                code="member_coverage_unknown",
                severity="info",
                message="Aggregated at the source: missing member series cannot be seen.",
                source="bucket_state",
            )
        )
    return Bundle(result.buckets, result.series, companions, caveats)
