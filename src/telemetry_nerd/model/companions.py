"""Companion series and how ops carry them (spec 2026-10-02 §3)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import polars as pl
import pyarrow as pa

from telemetry_nerd.analysis.exprkind import _close, _mask_strings, _strip_comments
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, compute
from telemetry_nerd.model.caveats import Caveat
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import parse_duration
from telemetry_nerd.sources.observed import counts_are_observed

Policy = Literal["carry", "recompute", "derive", "drop"]

SOURCE_AGGREGATED = re.compile(
    r"\b(sum|avg|min|max|count|group|stddev|stdvar|topk|bottomk|quantile|count_values)\b\s*"
    r"(by|without)?\s*(\([^)]*\))?\s*\(",
    re.IGNORECASE,
)


# VictoriaMetrics computes these from the sample before the window (VQ2, vm__pg_* fixtures):
# increase/increase_pure/delta return the whole gap's change in every bucket whose window reaches
# back over it; idelta returns the raw sample value, in the first bucket only. rate/irate/deriv/
# rate_over_sum leave that bucket empty or ignore the previous sample: not flagged.
_WINDOW_WIDE = ("increase_pure", "increase", "delta")
_PREVIOUS_SAMPLE_FUNCS = re.compile(
    r"\b(" + "|".join((*_WINDOW_WIDE, "idelta")) + r")\s*\(", re.IGNORECASE
)

_RANGE_BODY = re.compile(r"\s*((?:[0-9]+[a-z]+)+)\s*(?::[^\]]*)?")


def _bracket_duration(text: str, open_idx: int) -> int | None:
    """Range in ms of the `[...]` opening at `open_idx` (a subquery's step is ignored), None if
    it is not a literal duration."""
    try:
        body = text[open_idx + 1 : _close(text, open_idx)]
    except ValueError:
        return None
    m = _RANGE_BODY.fullmatch(body)
    try:
        return parse_duration(m.group(1)) if m else None
    except ValueError:
        return None


def _open_of(text: str, close_idx: int) -> int:
    depth = 0
    for i in range(close_idx, -1, -1):
        c = text[i]
        if c in ")]}":
            depth += 1
        elif c in "([{":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _top_level_bracket(text: str, start: int, end: int) -> int | None:
    depth = 0
    for i in range(start, end):
        c = text[i]
        if c == "[" and depth == 0:
            return i
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
    return None


def _enclosing_subquery_ms(text: str, call_open: int, call_close: int) -> int:
    """Sum of the ranges of subqueries `(...)[range:step]` whose parenthesised expression holds
    the call: its spike stays inside each of those windows for that much longer."""
    total = 0
    for m in re.finditer(r"\)\s*\[", text):
        bracket = m.end() - 1
        if ":" not in text[bracket : _safe_close(text, bracket)]:
            continue
        paren = _open_of(text, m.start())
        if 0 <= paren <= call_open and m.start() >= call_close:
            total += _bracket_duration(text, bracket) or 0
    return total


def _safe_close(text: str, idx: int) -> int:
    try:
        return _close(text, idx)
    except ValueError:
        return len(text)


def previous_sample_windows(expr: str) -> list[int | None]:
    """How far (ms) the spike of each increase/increase_pure/delta/idelta call reaches in the
    output: its own range (idelta: none) plus the ranges of subqueries around it; None where a
    range cannot be parsed. Empty when the expression has no such call."""
    text = _mask_strings(_strip_comments(expr))
    out: list[int | None] = []
    for call in _PREVIOUS_SAMPLE_FUNCS.finditer(text):
        try:
            end = _close(text, call.end() - 1)
        except ValueError:
            out.append(None)
            continue
        bracket = _top_level_bracket(text, call.end(), end)
        own = _bracket_duration(text, bracket) if bracket is not None else None
        if call.group(1).lower() == "idelta":
            own = own and 0
        out.append(None if own is None else own + _enclosing_subquery_ms(text, call.end() - 1, end))
    return out


def post_gap_buckets(expr: str, step_ms: int) -> int:
    """How many buckets after a gap carry the gap: a reach of w covers ceil(w / step) steps; the
    first bucket only if the range cannot be parsed or for idelta. 0: not applicable."""
    reaches = previous_sample_windows(expr)
    if not reaches:
        return 0
    return max([1, *(-(-w // step_ms) for w in reaches if w)])


# The companion kinds a dataset carries. With one kind there is nothing to dispatch on, so callers
# use its schema (`STATE_SCHEMA`), `coarsen` and `from_bucket_state` directly and this is only the
# list `OPS` must declare a policy against (spec §3.3). A second kind is the time to give each one a
# registry entry (schema, merge, coarsen, caveats) that callers go through.
KINDS = ("bucket_state",)

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


def derive_states(meta: DatasetMeta, result: FetchResult) -> pa.Table:
    """bucket_state of a stored dataset: pure function of its buckets, failed spans, expression
    and the semantics hints recorded at query time."""
    mode = "presence" if meta.representation == "quantile" else "samples"
    failed = [(a, b, str(reason)) for a, b, reason in meta.failed_spans]
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
            if meta.semantics_flags.get("post_gap_increase_spike")
            else 0
        ),
    )


def dataset_bundle(store: DatasetStore, meta: DatasetMeta, result: FetchResult) -> Bundle:
    # a code output's counts are whatever the code gave (often unknown): coverage of its inputs
    # is not carried through code, and judging it from those counts would invent it
    op = meta.derived["op"] if meta.derived else "code" if meta.code_node else "query"
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
