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

from telemetry_nerd.analysis.exprkind import counter_rate_source

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
    metric = (sel if brace < 0 else sel[:brace]).strip()
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
