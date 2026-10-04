"""Companion series and how ops carry them (spec 2026-10-02 §3)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Literal

import polars as pl
import pyarrow as pa
import pyarrow.compute as pc

from telemetry_nerd.analysis.exprkind import (
    _close,
    _mask_strings,
    _strip_comments,
    range_windows_ms,
)
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.bucket_state import STATE_SCHEMA, State, compute
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
    return _states(
        result,
        expr=meta.expr,
        start_ms=meta.start_ms,
        end_ms=meta.end_ms,
        step_ms=meta.step_ms,
        resolution_ms=meta.resolution_ms,
        failed=[(a, b, str(reason)) for a, b, reason in meta.failed_spans],
        semantics_flags=meta.semantics_flags,
        mode="presence" if meta.representation == "quantile" else "samples",
    )


def _states(
    result: FetchResult,
    *,
    expr: str,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    resolution_ms: int,
    failed: list[tuple[int, int, str]],
    semantics_flags: dict,
    mode: Literal["samples", "presence"] = "samples",
) -> pa.Table:
    return compute(
        result.buckets,
        result.series["series_id"].to_pylist(),
        start_ms=start_ms,
        end_ms=end_ms,
        step_ms=step_ms,
        resolution_ms=resolution_ms,
        mode=mode,
        failed=failed,
        # counts that are subquery evaluations (lookback-filled) cannot show coverage
        source_filled=not counts_are_observed(expr),
        post_gap_buckets=(
            post_gap_buckets(expr, step_ms) if semantics_flags.get("post_gap_increase_spike") else 0
        ),
        # an assumed (default) resolution is no evidence of the series' interval (principle 15)
        interval_known=bool(semantics_flags.get("series_interval_known")),
    )


_TILE_FUNCS = re.compile(r"\b(" + "|".join(_WINDOW_WIDE) + r")\s*\(", re.IGNORECASE)
_LABEL_LISTS = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)", re.IGNORECASE
)
_OFFSET = re.compile(r"\boffset\s+-?[0-9a-z]+", re.IGNORECASE)
_NOT_SELECTORS = frozenset(
    [
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "bool",
        "and",
        "or",
        "unless",
        "offset",
        "inf",
        "nan",
    ]
)


def _instant_operand(expr: str) -> bool:
    """Whether a selector of the expression is read without a range window (an instant reading:
    lookback fills it in a bucket without samples)."""
    text = re.sub(r"\{[^{}]*\}", "{}", _mask_strings(_strip_comments(expr)))
    text = _OFFSET.sub(" ", _LABEL_LISTS.sub(" ", text))
    text = re.sub(r"\[[^\]]*\]", "[]", text)
    for m in re.finditer(r"[a-zA-Z_:][\w:]*|\{\}", text):
        before = text[: m.start()].rstrip()
        if m.start() and (text[m.start() - 1].isalnum() or text[m.start() - 1] in "_:."):
            continue  # inside a number or a longer name
        rest = text[m.end() :].lstrip()
        if m.group() == "{}":
            if before and (before[-1].isalnum() or before[-1] in "_:"):
                continue  # the matchers of a named selector, judged with its name
        elif m.group().lower() in _NOT_SELECTORS or rest.startswith("("):
            continue
        elif rest.startswith("{}"):
            rest = rest[2:].lstrip()
        if not rest.startswith("["):
            return True
    return False


def _unobserved_rule(expr: str, step_ms: int) -> tuple[int | None, bool] | None:
    """(shortest range window longer than the step, whether it reads increase() tiles), or None
    when a value without a sample is never the expression's own; see unobserved_values_hold."""
    windows = range_windows_ms(expr)
    if not windows or _instant_operand(expr):
        return None
    short = [w for w in windows if w <= step_ms]
    wide = [w for w in windows if w > step_ms]
    if short:
        if any(w != step_ms for w in short):
            return None
        text = _mask_strings(_strip_comments(expr))
        tiles = 0
        for call in _TILE_FUNCS.finditer(text):
            end = _safe_close(text, call.end() - 1)
            bracket = _top_level_bracket(text, call.end(), end)
            own = _bracket_duration(text, bracket) if bracket is not None else None
            tiles += own == step_ms
        if tiles != len(short):
            return None
    return (min(wide) if wide else None), bool(short)


def unobserved_values_hold(expr: str, step_ms: int) -> bool:
    """Whether the value an expression has in a bucket that observed no sample of its own can be
    the expression's real value there, not a fill (uup). True when no selector is read without a
    range window and each window either reaches past the bucket (rate(x[1m]) at a 15 s step:
    computed from the samples inside it, when it holds one) or is an increase/increase_pure/delta
    tile exactly one step long (a tile without a sample increased by 0, from the last sample
    before it to the last one in it, and a later tile carries the change: the tiles partition
    the counter). False for an instant reading (a lookback fill), a window shorter than the step
    and any other function over a window that can hold no sample (VictoriaMetrics returns rate()
    0 there, which no sample supports). settle_unobserved checks the samples behind each value."""
    return _unobserved_rule(expr, step_ms) is not None


def _supported(df: pl.DataFrame, wide_ms: int | None, tiles: bool, step_ms: int) -> pl.Series:
    """Per row: whether a count-0 row's value rests on samples. A wide window needs a sample in a
    bucket wholly inside the shortest one (the floor(w / step) - 1 buckets before); a tile needs
    a later sample carrying its change, or the bucket before it holding 2 (its scrape came early
    into that one)."""
    seen: dict[str, dict[int, int]] = {}
    for sid, ts, c in df.select("series_id", "ts_ms", "count").iter_rows():
        if c:
            seen.setdefault(sid, {})[ts] = c
    last = {sid: max(t) for sid, t in seen.items()}
    back = (wide_ms // step_ms - 1) if wide_ms is not None else 0
    out = []
    for sid, ts, c in df.select("series_id", "ts_ms", "count").iter_rows():
        if c != 0:
            out.append(True)
            continue
        s = seen.get(sid, {})
        ok = True
        if wide_ms is not None:
            ok = any(ts - k * step_ms in s for k in range(1, back + 1))
        if tiles:
            ok = ok and (ts < last.get(sid, ts) or s.get(ts - step_ms, 0) >= 2)
        out.append(ok)
    return pl.Series(out, dtype=pl.Boolean)


def settle_unobserved(
    result: FetchResult,
    *,
    expr: str,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    resolution_ms: int,
    semantics_flags: dict | None = None,
) -> FetchResult:
    """Decide the fetched buckets that carry a value but observed no sample (count 0; the source
    adapter keeps them, sources/promql.py). At about one sample per bucket a scrape near a bucket
    edge lands in the neighbouring bucket: bucket_state reads that bucket OK (spilled, spec §5.1);
    so it reads the buckets between a slower series' samples. There the value stays when it is
    the expression's own (`unobserved_values_hold`) and samples support it (`_supported`), marked
    by its count of 0: dropping it would leave the neighbour, which holds two scrapes' worth of
    an increase, against one bucket fewer (Little's law's lambda read 1.4-1.6x high that way,
    9178611). Elsewhere (a gap: EMPTY / ABSENT / UNKNOWN, a fill such as lookback, a window
    holding no sample, a tile no later sample carries) it is missing data and is dropped: never
    a fabricated value or zero."""
    buckets = result.buckets
    if buckets.num_rows == 0:
        return result
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    unobserved = pl.col("count") == 0
    if not df.select(unobserved.any()).item():
        return result
    rule = _unobserved_rule(expr, step_ms)
    if rule is not None and counts_are_observed(expr):
        states = pl.from_arrow(
            _states(
                result, expr=expr, start_ms=start_ms, end_ms=end_ms, step_ms=step_ms,
                resolution_ms=resolution_ms, failed=list(result.failed),
                semantics_flags=semantics_flags or {},
            )
        )  # fmt: skip
        assert isinstance(states, pl.DataFrame)
        ok = states.filter(pl.col("state") == int(State.OK)).select(
            "series_id", "ts_ms", pl.lit(True).alias("_ok")
        )
        df = df.join(ok, on=["series_id", "ts_ms"], how="left").sort("series_id", "ts_ms")
        df = df.with_columns(_supported(df, *rule, step_ms).alias("_supported"))
        keep = ~unobserved | (pl.col("_ok").fill_null(False) & pl.col("_supported"))
        df = df.filter(keep).drop("_ok", "_supported")
    else:
        df = df.filter(~unobserved)
    kept = df.select(buckets.schema.names).to_arrow().cast(buckets.schema)
    ids = pa.array(df["series_id"].unique().to_list(), pa.string())
    series = result.series.filter(pc.is_in(result.series["series_id"], value_set=ids))
    return replace(result, buckets=kept, series=series)


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
