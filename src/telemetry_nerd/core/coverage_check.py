"""Does a claim window have the data to support a claim? (spec 2026-10-02 §4.4)

Coverage is judged per series, over the evidence series the claim is about (those its
`scope.selector` names, `claim_scope.claim_series`), never pooled: a silent series is not hidden by
healthy ones, and trouble on one series does not block a claim about another.

One series: the whole claim window counts. Below `BLOCK_BELOW` of its expected samples, or any
`unknown` bucket, blocks the claim; less than all of them warns. Buckets before its first sample
and after its last one are not observed either (it may not have existed yet, may have left, or may
return just outside the window: the data cannot tell), so they count as missing.

Several series: each is judged over its own span inside the window, from its first sample to its
last. A series first seen inside the window, or with no samples from some time on, is named (with
the alternatives, and a hint to check membership over a wider window) but not counted as missing.
A member is *without data* when it has no samples there (silent), under `BLOCK_BELOW` of its
expected samples (low), or any `unknown` bucket (untrusted). Members without data are named in a
warning; the claim is blocked only when it cannot rest on the rest: when more than
`MAX_WITHOUT_DATA` of the members alive in the window are without data, or none has a sample.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import polars as pl
import pyarrow as pa

from telemetry_nerd.core.claim_scope import ClaimSeries, claim_series
from telemetry_nerd.model.bucket_state import Flag, State
from telemetry_nerd.model.caveats import (
    MAX_WHERE_SERIES,
    UNOBSERVABLE_MESSAGE,
    Caveat,
    Severity,
    Where,
    _total,
    runs,
    series_name,
)
from telemetry_nerd.model.time import iso

BLOCK_BELOW = 0.5  # share of expected samples under which a series cannot support a claim
MAX_WITHOUT_DATA = 0.5  # share of a claim's series without data above which it is blocked
MAX_NAMED = 10  # series named per list in a message (where.series keeps MAX_WHERE_SERIES)
# A run of empty buckets bounded by samples this long or longer may be the series leaving and
# rejoining (a pod restarted or rescheduled), not only lost scrapes. 5 min is Prometheus' default
# lookback delta: past it the source itself stops returning the series at an instant, i.e. treats
# it as gone; shorter runs are a few lost scrapes at any common scrape interval.
LONG_GAP_MS = 300_000

Labels = Mapping[str, Mapping[str, str]]
Namer = Callable[[str], str]


def claim_coverage(
    states: pa.Table,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    *,
    labels: Labels | None = None,
    selector: str | None = None,
    metric: str | None = None,
) -> list[Caveat]:
    """Caveats on the claim window (start, end] from a dataset's bucket_state. `labels`: series id
    -> labels (default: every series in `states`, unlabelled); `selector`: the claim's
    scope.selector; `metric`: the dataset's metric name when its expression has one."""
    df = pl.from_arrow(states)
    if labels is None:
        labels = {sid: {} for sid in df["series_id"].unique().to_list()}
    scope = claim_series(selector, labels, metric)
    if scope.mismatch:
        return [Caveat(code="missing_data", severity="blocks_claim",
                       where=Where(spans=[(start_ms, end_ms)]), source="validator",
                       message=f"The claim names series this evidence does not contain: "
                       f"{scope.mismatch}.")]  # fmt: skip
    df = df.filter(pl.col("series_id").is_in(scope.ids))
    return [
        *_verdict(df, scope, labels, start_ms, end_ms, step_ms, selector),
        *_post_gap(df, scope.ids, labels, start_ms, end_ms, step_ms),
    ]


def _in_window(start_ms: int, end_ms: int, step_ms: int) -> pl.Expr:
    """A bucket ending at t covers (t - step, t]; it counts when that overlaps the claim window."""
    return (pl.col("ts_ms") > start_ms) & (pl.col("ts_ms") - step_ms < end_ms)


def _post_gap(
    df: pl.DataFrame, ids: Sequence[str], labels: Labels, start_ms: int, end_ms: int, step_ms: int
) -> list[Caveat]:
    """Values right after a gap that the source computed from before the gap (not real spikes)."""
    hit = df.filter(
        _in_window(start_ms, end_ms, step_ms)
        & (pl.col("state") != int(State.UNKNOWN))
        & ((pl.col("flags") & int(Flag.POST_GAP)) != 0)
    )
    if hit.is_empty():
        return []
    spans = runs(hit["ts_ms"].unique().to_list(), step_ms)
    sids = sorted(hit["series_id"].unique().to_list())
    one = len(ids) == 1
    on = "" if one else f" on {_names(sids, _namer(ids, labels))}"
    return [Caveat(code="post_gap_spike", severity="warn", source="validator",
                   where=Where(spans=spans, series=None if one else sids[:MAX_WHERE_SERIES]),
                   message=f"Values right after a gap ({_total(spans)} in the claim window{on}) "
                   "are computed from the sample before the gap, not real spikes; do not cite "
                   "them as one.")]  # fmt: skip


@dataclass
class _Member:
    """One series' verdict over the claim window."""

    sid: str
    share: float = 0.0  # Σobserved / Σexpected over the judged buckets that are not unknown
    unknown: bool = False
    partial: bool = False  # some judged bucket short of expected
    alive: bool = False  # has a judged bucket (several-series claims: inside its own span)
    sampled: bool = False  # has a sample in the window
    first_after: int | None = None  # first sample, when inside the window after its start
    last_before: int | None = None  # last sample in the data, when before the window's end
    long_gaps: list[tuple[int, int]] = field(default_factory=list)  # bounded, >= LONG_GAP_MS

    @property
    def lacking(self) -> str | None:
        """Why it cannot support the claim (silent / low / untrusted), or None."""
        if self.unknown:
            return "untrusted"
        if not self.sampled:
            return "silent"
        return "low" if self.share < BLOCK_BELOW else None


def _judge(g: pl.DataFrame, start_ms: int, end_ms: int, step_ms: int, own_span: bool) -> _Member:
    """`g`: one series' buckets over the whole dataset. `own_span`: judge only from its first
    sample to its last (several-series claims), else the whole window."""
    m = _Member(g["series_id"][0])
    seen = g.filter(pl.col("observed") > 0)["ts_ms"]
    w = g.filter(_in_window(start_ms, end_ms, step_ms))
    if seen.len() and w.height:
        first, last = seen.min(), seen.max()
        if w["ts_ms"].min() < first <= w["ts_ms"].max():
            m.first_after = first
        if last < w["ts_ms"].max():
            m.last_before = last
        if own_span:
            w = w.filter(pl.col("ts_ms").is_between(first, last))
        # whole runs (also past the window edges), bounded by samples, that reach into the window
        empty = g.filter(
            (pl.col("state") == int(State.EMPTY)) & pl.col("ts_ms").is_between(first, last)
        )["ts_ms"]
        m.long_gaps = [
            (a, b)
            for a, b in runs(empty.to_list(), step_ms)
            if b - a >= LONG_GAP_MS and b > start_ms and a < end_ms
        ]
    elif own_span:
        w = w.filter(pl.col("state") != int(State.ABSENT))  # never sampled: all of it is silent
    m.alive = w.height > 0
    m.unknown = bool((w["state"] == int(State.UNKNOWN)).any())
    m.sampled = bool((w["observed"] > 0).any())
    known = w.filter(pl.col("state") != int(State.UNKNOWN))
    exp = known["expected"].sum()
    m.share = min(1.0, known["observed"].sum() / exp) if exp else 0.0
    m.partial = known.filter(pl.col("state") != int(State.OK)).height > 0
    return m


def _namer(ids: Sequence[str], labels: Labels) -> Namer:
    """Series -> a short name: only the labels that differ among the claim's series."""
    keys = {k for s in ids for k in labels.get(s, {})}
    varying = {k for k in keys if len({labels.get(s, {}).get(k) for s in ids}) > 1}

    def name(sid: str) -> str:
        lb = labels.get(sid) or {}
        return series_name({k: v for k, v in lb.items() if k in varying} or lb) if lb else sid

    return name


def _identity(ids: Sequence[str], labels: Labels) -> str | None:
    """The label that tells the claim's series apart best (most distinct values), e.g. pod."""
    keys = {k for s in ids for k in labels.get(s, {})}
    counts = {k: len({labels.get(s, {}).get(k) for s in ids}) for k in keys}
    best = max(counts.items(), key=lambda kv: (kv[1], kv[0]), default=None)
    return best[0] if best and best[1] > 1 else None


def _names(sids: Sequence[str], name: Namer) -> str:
    shown = ", ".join(name(s) for s in sids[:MAX_NAMED])
    return shown + (f" and {len(sids) - MAX_NAMED} more" if len(sids) > MAX_NAMED else "")


def _gap_texts(members: Sequence[_Member], name: Namer) -> list[str]:
    """Long bounded gaps, one sentence per distinct set of gaps (a shared outage is one)."""
    by_gaps: dict[tuple[tuple[int, int], ...], list[str]] = {}
    for m in members:
        if m.long_gaps:
            by_gaps.setdefault(tuple(m.long_gaps), []).append(m.sid)
    out = []
    for gaps, sids in list(by_gaps.items())[:MAX_NAMED]:
        when = ", ".join(f"{iso(a)}–{iso(b)[11:]}" for a, b in gaps)
        out.append(f"{_names(sids, name)} {'has' if len(sids) == 1 else 'have'} no samples "
                   f"{when} (scrape loss, or the series left and rejoined; not distinguished "
                   "yet)")  # fmt: skip
    if len(by_gaps) > MAX_NAMED:
        out.append(f"{len(by_gaps) - MAX_NAMED} more sets of such gaps")
    return out


def _lifecycle(m: _Member, name: Namer) -> list[str]:
    out = []
    if m.first_after is not None:
        out.append(f"{name(m.sid)}: first sample at {iso(m.first_after)}, inside the window (it "
                   "may not have existed before, or was not scraped)")  # fmt: skip
    if m.last_before is not None:
        out.append(f"{name(m.sid)}: last sample at {iso(m.last_before)}, none after it in the "
                   "data (it may have left, or may return after the window)")  # fmt: skip
    return out


def _churn_hint(ids: Sequence[str], labels: Labels, selector: str | None) -> str:
    ident = _identity(ids, labels)
    what = f"set({ident})" if ident else "the set of series"
    expr = f"count by ({ident}) ({selector})" if ident and selector else "a presence query"
    return (f"Membership changes within the window: check whether {what} per bucket is "
            f"consistent over a wider window (e.g. {expr} over a longer range).")  # fmt: skip


def _verdict(
    df: pl.DataFrame,
    scope: ClaimSeries,
    labels: Labels,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    selector: str | None,
) -> list[Caveat]:
    notes = ["".join(f" ({n}.)" for n in scope.notes)]  # said once, on the first caveat

    def caveat(code: str, severity: Severity, message: str, sids: Sequence[str] = ()) -> Caveat:
        where = Where(spans=[(start_ms, end_ms)], series=list(sids)[:MAX_WHERE_SERIES] or None)
        note, notes[0] = notes[0], ""
        return Caveat(code=code, severity=severity, where=where, source="validator",
                      message=message + note)  # fmt: skip

    w = df.filter(_in_window(start_ms, end_ms, step_ms))
    if w.is_empty():
        return [caveat("missing_data", "blocks_claim",
                       "No data from this dataset in the claim window.")]  # fmt: skip
    if ((w["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():
        return [caveat("untrusted_data", "blocks_claim", UNOBSERVABLE_MESSAGE + " Re-query a "
                       "simpler expression (e.g. split it into its selectors) instead of retrying "
                       "this one.")]  # fmt: skip
    groups = [g for _, g in df.sort("ts_ms").group_by("series_id", maintain_order=True)]
    if len(scope.ids) == 1:
        return _single(_judge(groups[0], start_ms, end_ms, step_ms, False), labels, caveat,
                       selector)  # fmt: skip
    members = sorted(
        (_judge(g, start_ms, end_ms, step_ms, True) for g in groups), key=lambda m: m.sid
    )
    return _several(members, scope.ids, labels, caveat, selector)


def _single(m: _Member, labels: Labels, caveat, selector: str | None) -> list[Caveat]:
    nm = series_name(labels[m.sid]) if labels.get(m.sid) else m.sid
    life = _lifecycle(m, lambda _: nm)
    extra = "".join(f" {s}." for s in life)
    gaps = "".join(f" {t}." for t in _gap_texts([m], lambda _: nm))
    if m.unknown:
        return [caveat("untrusted_data", "blocks_claim", f"{nm}: the claim window contains data "
                       "the source could not return (fetch failed or unknown).")]  # fmt: skip
    if not m.sampled:
        return [caveat("missing_data", "blocks_claim",
                       (gaps.lstrip() or f"{nm} has no samples in the claim window.")
                       + extra)]  # fmt: skip
    if m.share < BLOCK_BELOW:
        return [caveat("missing_data", "blocks_claim", f"{nm}: only {m.share:.0%} of expected "
                       f"samples in the claim window.{gaps}{extra}")]  # fmt: skip
    if m.partial or life:
        hint = f" {_churn_hint([m.sid], labels, selector)}" if life else ""
        return [caveat("missing_data", "warn", f"{nm}: {m.share:.0%} of expected samples in the "
                       f"claim window.{gaps}{extra}{hint}")]  # fmt: skip
    return []


def _several(
    members: list[_Member], ids: Sequence[str], labels: Labels, caveat, selector: str | None
) -> list[Caveat]:
    name = _namer(ids, labels)
    alive = [m for m in members if m.alive]
    n = len(alive)
    kinds = {
        "silent": "no samples",
        "low": f"under {BLOCK_BELOW:.0%} of expected samples",
        "untrusted": "data the source could not return",
    }
    by_kind = {k: [m.sid for m in alive if m.lacking == k] for k in kinds}
    lacking = [s for k in kinds for s in by_kind[k]]
    parts = "; ".join(f"{kinds[k]}: {_names(by_kind[k], name)}" for k in kinds if by_kind[k])
    partial = [m.sid for m in alive if m.lacking is None and m.partial]
    life = [t for m in members for t in _lifecycle(m, name)]
    gaps = _gap_texts(alive, name)
    detail = "".join(f" {s}." for s in [*gaps, *life[:MAX_NAMED]])
    if len(life) > MAX_NAMED:
        detail += f" ({len(life) - MAX_NAMED} more membership changes.)"
    hint = f" {_churn_hint(ids, labels, selector)}" if life else ""
    if not any(m.sampled for m in alive):
        return [caveat("missing_data", "blocks_claim", f"None of the {len(ids)} series the claim "
                       f"is about has samples in the claim window.{detail}{hint}",
                       ids)]  # fmt: skip
    if len(lacking) > MAX_WITHOUT_DATA * n:
        return [caveat("missing_data", "blocks_claim", f"{len(lacking)} of {n} series the claim is "
                       f"about lack data in the claim window ({parts}): more than "
                       f"{MAX_WITHOUT_DATA:.0%} of them, so the rest cannot carry it."
                       f"{detail}{hint}", lacking)]  # fmt: skip
    out = []
    if lacking:
        code = "untrusted_data" if len(by_kind["untrusted"]) == len(lacking) else "missing_data"
        out.append(caveat(code, "warn", f"{len(lacking)} of {n} series the claim is about lack "
                          f"data in the claim window ({parts}); the claim rests on the other "
                          f"{n - len(lacking)}.", lacking))  # fmt: skip
    if partial:
        out.append(caveat("missing_data", "warn", f"{len(partial)} of {n} series have fewer "
                          f"samples than expected in the claim window: {_names(partial, name)}.",
                          partial))  # fmt: skip
    if detail:
        sids = [m.sid for m in members if m.long_gaps or m.first_after or m.last_before]
        own = " Each series is judged over its own span in the window." if life else ""
        out.append(caveat("membership", "warn", f"{own}{detail}{hint}".lstrip(),
                          sids))  # fmt: skip
    return out
