"""Counter series born on their first event: when absence means zero (eval finding icm).

Missing data is unknown, never zero. The one exception this module encodes is a known
Prometheus/OpenTelemetry pattern: a labelled counter child does not exist until its first
increment. Prometheus client libraries create a `{code="500"}` child on the first 500
(https://prometheus.io/docs/practices/instrumentation/#avoid-missing-metrics); the OTel
span-metrics connector creates the `status_code="STATUS_CODE_ERROR"` series of a service/span on
its first error span, and drops series that stop updating when `metrics_expiration` is set
(https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/connector/spanmetricsconnector);
a process restart drops every child until it is incremented again. So `rate(errors[w])` has NO
series, not a 0, while nothing failed.

Absence alone cannot tell "no event yet" from "instrument down": both look the same. The guard is
a live sibling: the same metric and the same identity labels with another outcome (the OK / UNSET
/ 2xx series). Where the sibling reports at a step, the pipeline delivered this instrument's data
at that step, so this child's absence there means it did not exist: 0 events. Where the sibling is
absent too, the step stays unknown.

Only absence OUTSIDE the series' observed lifetime is read this way (before its first point: not
yet born; after its last: expired or reset by a restart). A gap between two observed points of a
series that already existed is missing data and stays a gap.

Pure: parsing, keys and numpy; fetching the sibling is the caller's job.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np

from telemetry_nerd.analysis.exprkind import (
    counter_rate_source,
    range_windows_ms,
    rate_functions,
    split_ratio,
)

#: outcome labels: their values partition one instrument's events into outcomes (status codes,
#: error/ok, result). A child per value is created on that value's first event.
OUTCOME_LABELS = frozenset(
    {
        "status_code",  # OTel span-metrics connector (STATUS_CODE_UNSET / OK / ERROR)
        "otel_status_code",
        "http_response_status_code",  # OTel semconv HTTP
        "http_status_code",
        "status",
        "code",  # Prometheus client conventions (http_requests_total{code="500"})
        "status_class",
        "rpc_grpc_status_code",  # OTel semconv RPC
        "grpc_code",  # go-grpc-prometheus
        "grpc_status",
        "outcome",  # Micrometer
        "result",
        "error",
        "error_type",
    }
)

CAVEAT = "absent_as_zero"
ASSUMPTION = (
    "measurement-system assumption: a counter series born on its first event (absent until "
    "then, or again after expiry / a restart) is read as 0 events at steps where its live "
    "sibling (same instrument and identity labels, another outcome) reports; where the sibling "
    "is absent too the step stays unknown, and gaps between observed points stay gaps"
)
ONSET_BIAS = (
    "rate()/increase() cannot see the jump from 0 to the first sample of a newly born series: "
    "the first events after birth are under-counted (never over-counted)"
)

_MATCHER = re.compile(
    r"""([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)"""
)
_NEGATE = {"=": "!=", "=~": "!~"}


def _metric(sel: str) -> str:
    """The metric name of a selector (empty when it has none)."""
    return sel.split("{", 1)[0].strip()


@dataclass(frozen=True)
class BornCounter:
    """An expression over a counter whose outcome children are born on their first event."""

    metric: str
    #: outcome labels the expression selects on or keeps
    outcome: tuple[str, ...]
    #: the live-sibling expression to fetch (the outcome matcher negated), or None when the
    #: siblings can only come from the dataset itself (the outcome label is kept in the output)
    complement: str | None


def born_counter(expr: str, type_of: Callable[[str], str | None]) -> BornCounter | None:
    """The born-on-first-event reading of `[sum [by (L)]] (rate|increase(counter{...}[w]))`
    that selects on or keeps an outcome label; None for any other expression."""
    src = counter_rate_source(expr)
    if src is None:
        return None
    sel, by = src
    brace = sel.find("{")
    metric = _metric(sel)
    if not metric or type_of(metric) != "counter":
        return None
    matchers = list(_MATCHER.finditer(sel[brace:])) if brace >= 0 else []
    positive = [m for m in matchers if m.group(1) in OUTCOME_LABELS and m.group(2) in _NEGATE]
    kept = [lab for lab in by if lab in OUTCOME_LABELS]
    if not positive and not kept and "*" not in by:
        return None  # summed over every outcome: a total, nothing born per outcome
    outcome = tuple(dict.fromkeys([m.group(1) for m in positive] + kept)) or ("*",)
    complement = None
    if len(positive) == 1:
        m = positive[0]
        a, b = brace + m.start(2), brace + m.end(2)
        sibling_sel = sel[:a] + _NEGATE[m.group(2)] + sel[b:]
        if expr.count(sel) == 1:
            complement = expr.replace(sel, sibling_sel, 1).strip()
    return BornCounter(metric, outcome, complement)


def sibling_key(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The instrument identity: the labels minus every outcome label."""
    return tuple(sorted((k, v) for k, v in labels.items() if k not in OUTCOME_LABELS))


@dataclass(frozen=True)
class Filled:
    ts: np.ndarray
    y: np.ndarray
    lead: int  # steps before the first observed point read as 0
    trail: int  # steps after the last observed point read as 0


def fill_absent(ts: np.ndarray, y: np.ndarray, alive: np.ndarray) -> Filled:
    """Zeros at the live-sibling steps (`alive`, sorted ts) outside [ts[0], ts[-1]]; interior
    gaps are left alone. `ts` must be sorted and non-empty."""
    lead = alive[alive < ts[0]]
    trail = alive[alive > ts[-1]]
    if not lead.size and not trail.size:
        return Filled(ts, y, 0, 0)
    out_ts = np.concatenate([lead, ts, trail]).astype(ts.dtype)
    out_y = np.concatenate([np.zeros(lead.size), y, np.zeros(trail.size)]).astype(float)
    return Filled(out_ts, out_y, int(lead.size), int(trail.size))


Series = dict[str, tuple[dict, np.ndarray, np.ndarray]]


def fill_born(
    series: Series, siblings: Mapping[str, tuple[dict, np.ndarray]] | None = None
) -> tuple[Series, dict[str, Filled]]:
    """Fill every series of `series` (sid -> labels, ts, y) from its live siblings: the other
    series of the same dataset with the same identity (`sibling_key`) plus `siblings` (sid ->
    labels, ts: the fetched complement). Returns the series and, per filled sid, what was
    filled."""
    alive: dict[tuple, dict[str, np.ndarray]] = {}
    for sid, (labels, ts, _) in series.items():
        alive.setdefault(sibling_key(labels), {})[sid] = ts
    extra: dict[tuple, list[np.ndarray]] = {}
    for labels, ts in (siblings or {}).values():
        extra.setdefault(sibling_key(labels), []).append(ts)
    out: Series = {}
    filled: dict[str, Filled] = {}
    for sid, (labels, ts, y) in series.items():
        key = sibling_key(labels)
        parts = [t for s, t in alive.get(key, {}).items() if s != sid] + extra.get(key, [])
        if not parts or not ts.size:
            out[sid] = (labels, ts, y)
            continue
        f = fill_absent(ts, y, np.unique(np.concatenate(parts)))
        out[sid] = (labels, f.ts, f.y)
        if f.lead or f.trail:
            filled[sid] = f
    return out, filled


def sibling_counts(
    series: Series, siblings: Series | None = None
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per series of `series`, its live siblings' counts summed per step (ts, y): the other
    series of the same dataset with the same identity plus `siblings` (the fetched complement,
    sid -> labels, ts, y). The traffic a born series' events are a thinning of: a dispersion
    source for the departure test's cautious model (analysis.diagnostics.Departure)."""
    pool: dict[tuple, list[tuple[str, np.ndarray, np.ndarray]]] = {}
    for sid, (labels, ts, y) in series.items():
        pool.setdefault(sibling_key(labels), []).append((sid, ts, y))
    for sid, (labels, ts, y) in (siblings or {}).items():
        pool.setdefault(sibling_key(labels), []).append(("\0" + sid, ts, y))
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sid, (labels, _, _) in series.items():
        parts = [(t, v) for s, t, v in pool.get(sibling_key(labels), []) if s != sid and t.size]
        if not parts:
            continue
        ts_all = np.concatenate([t for t, _ in parts])
        y_all = np.concatenate([np.asarray(v, float) for _, v in parts])
        uniq, inv = np.unique(ts_all, return_inverse=True)
        out[sid] = (uniq, np.bincount(inv, weights=y_all, minlength=uniq.size))
    return out


# ratios over a born counter (eval finding wr6j) ------------------------------------------------
RATIO_ASSUMPTION = (
    "measurement-system assumption: the numerator, a counter series born on its first event, is "
    "read as 0 events at steps where the denominator (the same instrument's events, every "
    "outcome or the others) reports > 0, outside the numerator's observed lifetime; there the "
    "ratio is 0, not missing. PromQL's division drops every step where the numerator series "
    "does not exist yet, so the ratio would be born at the fault like its counter"
)


@dataclass(frozen=True)
class BornRatio:
    """`A / B` over one counter: A selects one outcome (born on its first event), B the same
    instrument's events with the same grouping, function and window (all outcomes, or the
    other ones). B reporting at a step proves A's instrument alive there."""

    numerator: str
    denominator: str
    born: BornCounter  # the numerator's reading


def _matchers(sel: str) -> list[tuple[str, str, str]]:
    brace = sel.find("{")
    if brace < 0:
        return []
    return [(m.group(1), m.group(2), m.group(3)) for m in _MATCHER.finditer(sel[brace:])]


def rewrite_hint(numerator: str, denominator: str) -> str:
    """The exact rewrite that reads a born numerator as 0 where the denominator reports."""
    return (
        f"rewrite the ratio so the numerator is 0 where its series does not exist yet: "
        f"({numerator} or {denominator} * 0) / ({denominator}); or analyze the numerator alone "
        f"({numerator}): analyze reads a born counter as 0 against its live sibling"
    )


def born_ratio(expr: str, type_of: Callable[[str], str | None]) -> BornRatio | None:
    """The born-numerator reading of `A / B` (see BornRatio); None for any other expression."""
    parts = split_ratio(expr)
    if parts is None:
        return None
    a, b = parts
    sa, sb = counter_rate_source(a), counter_rate_source(b)
    born = born_counter(a, type_of)
    if sa is None or sb is None or born is None or born.complement is None:
        return None
    if set(sa[1]) != set(sb[1]) or range_windows_ms(a) != range_windows_ms(b):
        return None
    if rate_functions(a) != rate_functions(b) or _metric(sb[0]) != born.metric:
        return None
    ma, mb = _matchers(sa[0]), _matchers(sb[0])
    if {m for m in ma if m[0] not in OUTCOME_LABELS} != {m for m in mb if m[0] not in OUTCOME_LABELS}:  # fmt: skip
        return None
    outcome_a = {m for m in ma if m[0] in OUTCOME_LABELS}
    outcome_b = {m for m in mb if m[0] in OUTCOME_LABELS}
    if outcome_b and outcome_b != {(k, _NEGATE[op], v) for k, op, v in outcome_a if op in _NEGATE}:
        return None  # the denominator is neither every outcome nor the complement
    return BornRatio(a, b, born)


def fill_ratio(
    num: Series, den: Series
) -> tuple[Series, dict[str, Filled], dict[str, tuple[np.ndarray, np.ndarray]]]:
    """The ratio per denominator series (keyed and labelled as the denominator's), the
    numerator read as 0 outside its observed lifetime where the denominator reports > 0
    (fill_absent; its interior gaps stay gaps), at the steps where both are known and the
    denominator > 0. Also per series the (numerator, denominator) values behind each ratio
    point: the events and the traffic they are a share of."""
    by_key = {sibling_key(lab): (ts, y) for lab, ts, y in num.values()}
    out: Series = {}
    filled: dict[str, Filled] = {}
    behind: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sid, (labels, tb, yb) in den.items():
        yb = np.asarray(yb, float)
        ok = np.isfinite(yb) & (yb > 0)
        alive = np.asarray(tb)[ok]
        if not alive.size:
            continue
        got = by_key.get(sibling_key(labels))
        if got is None or not got[0].size:  # never born in the range: 0 wherever it reports
            f = Filled(alive, np.zeros(alive.size), int(alive.size), 0)
        else:
            f = fill_absent(np.asarray(got[0]), np.asarray(got[1], float), alive)
        common, ia, ib = np.intersect1d(f.ts, alive, return_indices=True)
        keep = np.isfinite(f.y[ia])
        a, b = f.y[ia][keep], yb[ok][ib][keep]
        out[sid] = (labels, common[keep], a / b)
        behind[sid] = (a, b)
        if f.lead or f.trail:
            filled[sid] = f
    return out, filled, behind
