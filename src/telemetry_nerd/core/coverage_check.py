"""Does a claim window have the data to support a claim? (spec 2026-10-02 §4.4)

Coverage is judged per series, over the evidence series the claim is about (those its
`scope.selector` names, `claim_scope.claim_series`), never pooled: a silent series is not hidden by
healthy ones, and trouble on one series does not block a claim about another.

One series: the whole claim window counts. Below `BLOCK_BELOW` of its expected samples, or any
`unknown` bucket, blocks the claim; less than all of them warns. Time before its first sample and
after its last one is unobserved too (it may not have existed yet, may have left, or may return
just outside the window: neither is provable from this evidence), so it counts against coverage.

Several series: each is judged over its own span inside the window, from its first sample to its
last when the silence after that lasts at least `LONG_GAP_MS` (shorter trailing silence is lost
scrapes and counts as missing). A series first seen inside the window, or with no samples since
some time, is named (with the alternatives, and a hint to check membership over a wider window)
but not counted as missing. A member is *without data* when it has no samples there (silent),
under `BLOCK_BELOW` of its expected samples (low), or any `unknown` bucket anywhere in the window
(untrusted: what happened there is not known, so neither is its span). Members without data are
named in a warning; the claim is blocked only when it cannot rest on the rest: when more than
`MAX_WITHOUT_DATA` of the members alive in the window are without data, or none has a sample.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import polars as pl
import pyarrow as pa

from telemetry_nerd.core.claim_scope import claim_series
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
# Silence this long or longer (no samples, bounded by samples or open at an edge) may be the series
# leaving and rejoining, or not existing at that time, not only lost scrapes. 5 min is Prometheus'
# default lookback delta: past it the source itself stops returning the series at an instant;
# shorter runs are a few lost scrapes at any common scrape interval. VictoriaMetrics has no fixed
# lookback (it derives one from the samples' own interval), so there it is a convention only.
LONG_GAP_MS = 300_000
# labels that name a member of a fleet, preferred when hinting at a membership check
IDENTITY_LABELS = ("pod", "instance", "host", "container", "node")
SCOPE_MISMATCH = "scope_mismatch"

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
    scope.selector; `metric`: the dataset's metric name when its expression has one. A selector
    naming no series of this dataset gives one `scope_mismatch` caveat (blocks_claim; a caller
    citing several datasets may skip this one instead)."""
    df = pl.from_arrow(states)
    if labels is None:
        labels = {sid: {} for sid in df["series_id"].unique().to_list()}
    scope = claim_series(selector, labels, metric)
    window = Where(spans=[(start_ms, end_ms)])
    if scope.mismatch:
        return [Caveat(code=SCOPE_MISMATCH, severity="blocks_claim", where=window,
                       source="validator", message=f"The claim names series this evidence does "
                       f"not contain: {scope.mismatch}.")]  # fmt: skip
    out = []
    if scope.notes:
        severity: Severity = "warn" if any(lv == "warn" for lv, _ in scope.notes) else "info"
        out.append(Caveat(code="claim_scope", severity=severity, where=window, source="validator",
                          message="; ".join(t for _, t in scope.notes) + "."))  # fmt: skip
    df = df.filter(pl.col("series_id").is_in(scope.ids))
    return [
        *_verdict(df, scope.ids, labels, start_ms, end_ms, step_ms, selector),
        *out,
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
    unknown: bool = False  # an unknown bucket anywhere in the window
    partial: bool = False  # some judged bucket short of expected
    alive: bool = False  # has a judged bucket (several-series claims: inside its own span)
    sampled: bool = False  # has a sample in the window
    first_after: int | None = None  # first sample, after >= LONG_GAP_MS of window without
    last_before: int | None = None  # last sample, >= LONG_GAP_MS before the data's end
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
    """`g`: one series' buckets over the whole dataset. `own_span`: judge only over its own span
    (several-series claims), else the whole window. Unknown counts over the whole window: it
    beats absent in bucket_state, so a failed fetch before the first sample hides whether there
    were samples, and what the series' span is."""
    m = _Member(g["series_id"][0])
    w = g.filter(_in_window(start_ms, end_ms, step_ms))
    unknown_ts = w.filter(pl.col("state") == int(State.UNKNOWN))["ts_ms"]
    m.unknown = unknown_ts.len() > 0
    seen = g.filter(pl.col("observed") > 0)["ts_ms"]
    if seen.len() and w.height:
        first, last = seen.min(), seen.max()
        w_lo, w_hi = w["ts_ms"].min(), w["ts_ms"].max()
        # silence at an edge is a membership question only when long, and when no unknown bucket
        # sits in it (then it is not known to be silence)
        lead = w_lo < first <= w_hi and first - w_lo >= LONG_GAP_MS
        if lead and not (unknown_ts < first).any():
            m.first_after = first
        trail = last < w_hi and g["ts_ms"].max() - last >= LONG_GAP_MS
        if trail and not (unknown_ts > last).any():
            m.last_before = last
        if own_span:
            hi = last if m.last_before is not None else w_hi
            w = w.filter(pl.col("ts_ms").is_between(first, hi))
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
    m.alive = w.height > 0 or m.unknown
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
    """The label that names the claim's members: a known identity label (pod, instance, ...)
    first, else the one with the most distinct values. For one series, a known one it carries."""
    keys = {k for s in ids for k in labels.get(s, {})}
    counts = {k: len({labels.get(s, {}).get(k) for s in ids}) for k in keys}
    for k in IDENTITY_LABELS:
        if k in counts and (counts[k] > 1 or len(ids) == 1):
            return k
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
        when = ", ".join(f"{iso(a)}–{iso(b)}" for a, b in gaps)
        out.append(f"{_names(sids, name)} {'has' if len(sids) == 1 else 'have'} no samples "
                   f"{when} (scrape loss, or the series left and rejoined; not distinguished "
                   "yet)")  # fmt: skip
    if len(by_gaps) > MAX_NAMED:
        out.append(f"{len(by_gaps) - MAX_NAMED} more sets of such gaps")
    return out


def _lifecycle(m: _Member, name: Namer) -> list[str]:
    out = []
    if m.first_after is not None:
        out.append(f"{name(m.sid)}: no samples before {iso(m.first_after)} in this evidence (it "
                   "may not have existed yet, or was not scraped)")  # fmt: skip
    if m.last_before is not None:
        out.append(f"{name(m.sid)}: no samples since {iso(m.last_before)} in this evidence (it "
                   "may have left, or may return after the window)")  # fmt: skip
    return out


def _churn_hint(ids: Sequence[str], labels: Labels, selector: str | None) -> str:
    ident = _identity(ids, labels)
    query = f"count by ({ident}) ({selector})" if ident and selector else None
    wider = f" (e.g. {query} over a longer range)" if query else ""
    if len(ids) == 1:
        return f"To tell, check whether it has samples over a wider window{wider}."
    what = f"set({ident})" if ident else "the set of series"
    return (f"Membership changes within the window: check whether {what} per bucket is "
            f"consistent over a wider window{wider}.")  # fmt: skip


def _verdict(
    df: pl.DataFrame,
    ids: Sequence[str],
    labels: Labels,
    start_ms: int,
    end_ms: int,
    step_ms: int,
    selector: str | None,
) -> list[Caveat]:
    def caveat(code: str, severity: Severity, message: str, sids: Sequence[str] = ()) -> Caveat:
        where = Where(spans=[(start_ms, end_ms)], series=list(sids)[:MAX_WHERE_SERIES] or None)
        return Caveat(code=code, severity=severity, where=where, source="validator",
                      message=message)  # fmt: skip

    w = df.filter(_in_window(start_ms, end_ms, step_ms))
    if w.is_empty():
        return [caveat("missing_data", "blocks_claim",
                       "No data from this dataset in the claim window.")]  # fmt: skip
    if ((w["flags"] & int(Flag.SOURCE_FILLED)) != 0).any():
        return [caveat("untrusted_data", "blocks_claim", UNOBSERVABLE_MESSAGE + " Re-query a "
                       "simpler expression (e.g. split it into its selectors) instead of retrying "
                       "this one.")]  # fmt: skip
    groups = [g for _, g in df.sort("ts_ms").group_by("series_id", maintain_order=True)]
    if len(ids) == 1:
        m = _judge(groups[0], start_ms, end_ms, step_ms, False)
        return _single(m, labels, caveat, selector)
    members = sorted(
        (_judge(g, start_ms, end_ms, step_ms, True) for g in groups), key=lambda m: m.sid
    )
    return _several(members, ids, labels, caveat, selector)


def _single(m: _Member, labels: Labels, caveat, selector: str | None) -> list[Caveat]:
    nm = series_name(labels[m.sid]) if labels.get(m.sid) else m.sid
    life = _lifecycle(m, lambda _: nm)
    hint = f" {_churn_hint([m.sid], labels, selector)}" if life else ""
    extra = "".join(f" {s}." for s in [*_gap_texts([m], lambda _: nm), *life]) + hint
    if m.unknown:
        return [caveat("untrusted_data", "blocks_claim", f"{nm}: the claim window contains data "
                       "the source could not return (fetch failed or unknown).")]  # fmt: skip
    if not m.sampled:
        return [caveat("missing_data", "blocks_claim",
                       f"{nm} has no samples in the claim window.{extra}")]  # fmt: skip
    if m.share < BLOCK_BELOW:
        return [caveat("missing_data", "blocks_claim", f"{nm}: only {m.share:.0%} of expected "
                       f"samples in the claim window.{extra}")]  # fmt: skip
    if m.partial or life:
        return [caveat("missing_data", "warn", f"{nm}: {m.share:.0%} of expected samples in the "
                       f"claim window.{extra}")]  # fmt: skip
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
    shown = "".join(f" {s}." for s in life[:MAX_NAMED])
    if len(life) > MAX_NAMED:
        shown += f" ({len(life) - MAX_NAMED} more membership changes.)"
    membership = (
        f"Each series is judged over its own span in the window.{shown} "
        f"{_churn_hint(ids, labels, selector)}"
        if life
        else ""
    )
    gaps = "".join(f" {s}." for s in _gap_texts(alive, name))
    if n == 0:  # every one outside its own span here (an over-half count covers the rest)
        return [caveat("missing_data", "blocks_claim", f"None of the {len(ids)} series the claim "
                       f"is about has samples in the claim window.{gaps}"
                       f"{' ' + membership if membership else ''}", ids)]  # fmt: skip
    if len(lacking) > MAX_WITHOUT_DATA * n:
        return [caveat("missing_data" if len(by_kind["untrusted"]) < len(lacking)
                       else "untrusted_data", "blocks_claim",
                       f"{len(lacking)} of {n} series the claim is about lack data in the claim "
                       f"window ({parts}): more than {MAX_WITHOUT_DATA:.0%} of them, so the rest "
                       f"cannot carry it.{gaps}", lacking)]  # fmt: skip
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
    if gaps:
        out.append(caveat("long_gap", "warn", f"Long gaps (≥ 5m, samples on both sides):"
                          f"{gaps}", [m.sid for m in alive if m.long_gaps]))  # fmt: skip
    if membership:
        sids = [m.sid for m in members if m.first_after or m.last_before]
        out.append(caveat("membership", "warn", membership, sids))
    return out
