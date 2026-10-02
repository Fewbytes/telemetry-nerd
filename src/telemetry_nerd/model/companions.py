"""Companion series and how ops carry them (spec 2026-10-02 §3)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.bucket_state import STATE_SCHEMA, coarsen, compute
from telemetry_nerd.model.caveats import Caveat, from_bucket_state
from telemetry_nerd.model.series import FetchResult

Policy = Literal["carry", "recompute", "derive", "drop"]

SOURCE_AGGREGATED = re.compile(
    r"\b(sum|avg|min|max|count|group|stddev|stdvar|topk|bottomk|quantile|count_values)\b\s*"
    r"(by|without)?\s*(\([^)]*\))?\s*\(",
    re.IGNORECASE,
)


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


def _derive_states(meta, result: FetchResult) -> pa.Table:
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
    )


def dataset_bundle(store, meta, result: FetchResult) -> Bundle:
    op = meta.derived["op"] if meta.derived else "query"
    caveats: list[Caveat] = []
    companions: dict[str, pa.Table] = {}
    p = policy(op, "bucket_state")
    dropped = False
    if p == "derive":
        companions["bucket_state"] = _derive_states(meta, result)
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
