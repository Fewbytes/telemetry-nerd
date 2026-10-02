"""Model views (bead czt.3): how each role of a USE / RED / Little's law binding is drawn. Pure.

The chart form of a role follows its catalog semantics, never a generic line:

* rate / arrival_rate: the per-second rate of a counter, or of a histogram's observation count;
* RED errors: errors / requests with a Wilson band. Either a dedicated error counter over the
  rate role's requests, or the request metric split by the status matcher of the suggestion's expr
  hint (or one the caller names). Both sides are summed by the binding's join_on labels, so they
  line up member by member;
* duration / latency from a histogram: the distribution itself (heatmap; percentile bands are a
  view of the same payload), never an average of percentiles. A summary is drawn as its mean;
* utilization: its natural 0..1 axis;
* saturation: kept per series when a `bounded_by` relation exists, so the limit line lines up;
* concurrency: the gauge (requests in flight);
* USE errors: the rate of resource errors.

Label names in hint expressions are conventions (binding_suggest says so); nothing here reads the
source. `with_matchers` narrows every catalogued metric in an expression to the caller's labels.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pyarrow as pa

from telemetry_nerd.analysis.fraction import wilson
from telemetry_nerd.catalog.relations import BINDING_ROLES
from telemetry_nerd.model.series import BUCKET_SCHEMA, FetchResult

RI = "$__rate_interval"
#: a distribution with more members than this is merged (histograms merge exactly) into one
DIST_FACETS_MAX = 4
#: the Wilson interval drawn around an error ratio
RATIO_LEVEL = 0.95
RATIO_METHOD = "Wilson score on counts = mean rate x step"

_HINT_WINDOW = re.compile(r"\[5m\]")
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MetricInfo:
    """What the catalog knows about a role's metric."""

    name: str
    type: str | None  # counter | gauge | histogram | summary | None (unknown)
    native: bool = False  # a native histogram: no _bucket/_count series
    bounded: bool = False  # the catalog has a bounded_by relation from it


@dataclass(frozen=True)
class Hint:
    """A binding_suggest role detail: its form and expr hint (labels are conventions)."""

    form: str
    expr: str | None = None


@dataclass(frozen=True)
class RolePlan:
    role: str
    metric: str
    #: rate | error_ratio | errors | distribution | mean | value | utilization | saturation |
    #: concurrency
    form: str
    question: str
    expr: str | None = None  # line forms
    selector: str | None = None  # distribution: the histogram selector
    by: tuple[str, ...] = ()  # distribution: members
    num: str | None = None  # error_ratio: errors per second
    den: str | None = None  # error_ratio: requests per second
    notes: tuple[str, ...] = ()
    #: the role cannot be drawn as asked (e.g. an error split with no known status label)
    unresolved: str | None = None
    unit: str | None = None
    bounds: tuple[float, float] | None = None  # natural axis the role itself implies


ROLE_QUESTIONS: dict[tuple[str, str], str] = {
    ("RED", "rate"): "How many requests per second does {key} serve?",
    ("RED", "errors"): "What share of {key}'s requests fail?",
    ("RED", "duration"): "How long do {key}'s requests take?",
    ("littles_law", "arrival_rate"): "How fast do requests arrive at {key} (λ)?",
    ("littles_law", "latency"): "How long does a request spend in {key} (W)?",
    ("littles_law", "concurrency"): "How many requests are in flight in {key} (L)?",
    ("USE", "utilization"): "How busy is {key} (share of its capacity in use)?",
    ("USE", "saturation"): "How much work is waiting for {key}?",
    ("USE", "errors"): "How often does {key} report errors?",
}


def role_question(kind: str, role: str, key: str) -> str:
    return ROLE_QUESTIONS.get((kind, role), f"{kind} {role} of {{key}}").format(key=key)


def matcher_text(matchers: Mapping[str, str]) -> str:
    for k in matchers:
        if not _LABEL.match(k):
            raise ValueError(f"matchers: {k!r} is not a label name")
    return ",".join(f"{k}={json.dumps(v)}" for k, v in sorted(matchers.items()))


def sel(name: str, matchers: Mapping[str, str], extra: str | None = None) -> str:
    inner = ",".join(p for p in (matcher_text(matchers), extra or "") if p)
    return f"{name}{{{inner}}}" if inner else name


def with_matchers(expr: str, names: Iterable[str], matchers: Mapping[str, str]) -> str:
    """Narrow every occurrence of the given metric names in `expr` to `matchers`, merging into an
    existing `{...}`. Names are matched whole (not as a prefix of a longer name)."""
    text = matcher_text(matchers)
    if not text:
        return expr
    alts = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    if not alts:
        return expr
    pat = re.compile(rf"(?<![A-Za-z0-9_:])({alts})(?![A-Za-z0-9_:])(\{{[^{{}}]*\}})?")

    def repl(m: re.Match[str]) -> str:
        inner = (m.group(2) or "{}")[1:-1].strip()
        return f"{m.group(1)}{{{inner + ',' if inner else ''}{text}}}"

    return pat.sub(repl, expr)


def hint_expr(hint: Hint | None) -> str | None:
    """A usable query from a hint: no trailing comment, the suggester's fixed 5m window replaced
    by the step-aware rate interval. Histogram/summary hints are descriptions, not queries."""
    if hint is None or not hint.expr or hint.form in ("histogram", "summary"):
        return None
    return _HINT_WINDOW.sub(f"[{RI}]", hint.expr.split("#", 1)[0].strip()) or None


def split_matcher(hint: Hint | None) -> str | None:
    """The status matcher of a label_split hint, e.g. `http_response_status_code=~"5.."`."""
    if hint is None or not hint.expr:
        return None
    found = re.search(r"\{([^{}]*)\}", hint.expr)
    return found.group(1).strip() if found else None


def _by(join_on: Iterable[str]) -> str:
    j = list(join_on)
    return f" by ({', '.join(j)})" if j else ""


def per_second(m: MetricInfo, matchers: Mapping[str, str], extra: str | None = None) -> str:
    """Events per second of one metric (unaggregated): a counter's rate, a histogram's
    observation-count rate; a gauge is taken as already per second."""
    if m.type == "histogram" and m.native:
        return f"histogram_count(rate({sel(m.name, matchers, extra)}[{RI}]))"
    if m.type in ("histogram", "summary"):
        return f"rate({sel(m.name + '_count', matchers, extra)}[{RI}])"
    if m.type == "gauge":
        return sel(m.name, matchers, extra)
    return f"rate({sel(m.name, matchers, extra)}[{RI}])"


def summed(inner: str, join_on: Iterable[str]) -> str:
    return f"sum{_by(join_on)} ({inner})"


def plan_role(
    kind: str,
    role: str,
    m: MetricInfo,
    *,
    key: str,
    join_on: Iterable[str] = (),
    matchers: Mapping[str, str] | None = None,
    hint: Hint | None = None,
    rate_metric: MetricInfo | None = None,
    error_matcher: str | None = None,
    names: Iterable[str] = (),
) -> RolePlan:
    """How one bound role is drawn. `names`: catalogued metric names a hint expr may mention."""
    J = tuple(join_on)
    mt = dict(matchers or {})
    q = role_question(kind, role, key)
    hexpr = hint_expr(hint)
    if hexpr is not None:
        hexpr = with_matchers(hexpr, {m.name, *names}, mt)
    notes: list[str] = []
    if m.type is None:
        notes.append(f"{m.name}: type unknown to the catalog; drawn as a {_assumed(role)}")

    if role in ("rate", "arrival_rate"):
        if m.type in ("histogram", "summary"):
            notes.append(f"rate of {m.name}'s observation count")
        if m.type == "gauge":
            notes.append(f"{m.name} is a gauge: taken as already per second")
        return RolePlan(
            role, m.name, "rate", q, expr=summed(per_second(m, mt), J), notes=tuple(notes)
        )

    if role == "errors" and kind == "RED":
        split = (hint is not None and hint.form == "label_split") or (
            rate_metric is not None and m.name == rate_metric.name
        )
        if split:
            matcher = error_matcher or split_matcher(hint)
            if not matcher or "<" in matcher:
                return RolePlan(
                    role, m.name, "error_ratio", q,
                    unresolved=(
                        f"errors are a split of {m.name} by its status label, which the catalog "
                        'does not name: pass error_matcher (e.g. status_code=~"5..")'
                    ),
                )  # fmt: skip
            notes.append(f"errors: {m.name} where {matcher} (label names are conventions)")
            return RolePlan(
                role, m.name, "error_ratio", q,
                num=summed(per_second(m, mt, matcher), J), den=summed(per_second(m, mt), J),
                notes=tuple(notes), unit="ratio", bounds=(0.0, 1.0),
            )  # fmt: skip
        num = summed(per_second(m, mt), J)
        if rate_metric is None:
            notes.append("no request-rate role: errors are drawn as a rate, not a share")
            return RolePlan(role, m.name, "errors", q, expr=num, notes=tuple(notes))
        notes.append(f"errors: {m.name} over the requests of {rate_metric.name}")
        return RolePlan(
            role, m.name, "error_ratio", q, num=num, den=summed(per_second(rate_metric, mt), J),
            notes=tuple(notes), unit="ratio", bounds=(0.0, 1.0),
        )  # fmt: skip

    if role in ("duration", "latency"):
        if m.type == "histogram":
            selector = sel(m.name if m.native else m.name + "_bucket", mt)
            return RolePlan(
                role, m.name, "distribution", q, selector=selector, by=J, notes=tuple(notes)
            )
        if m.type == "summary":
            notes.append(
                f"{m.name} is a summary: its quantiles cannot be merged across instances, so its "
                "mean (sum / count) is drawn"
            )
            by = _by(J)
            expr = (
                f"sum{by} (rate({sel(m.name + '_sum', mt)}[{RI}])) / "
                f"sum{by} (rate({sel(m.name + '_count', mt)}[{RI}]))"
            )
            return RolePlan(role, m.name, "mean", q, expr=expr, notes=tuple(notes))
        notes.append(
            f"{m.name} is not a histogram: drawn per series as reported, never averaged across "
            "series"
        )
        return RolePlan(role, m.name, "value", q, expr=sel(m.name, mt), notes=tuple(notes))

    if role == "utilization":
        expr = hexpr or (
            f"rate({sel(m.name, mt)}[{RI}])" if m.type == "counter" else sel(m.name, mt)
        )
        return RolePlan(role, m.name, "utilization", q, expr=expr, notes=tuple(notes))

    if role == "saturation":
        plain = f"rate({sel(m.name, mt)}[{RI}])" if m.type == "counter" else sel(m.name, mt)
        if m.bounded:
            notes.append(f"{m.name} has a bounded_by limit: kept per series so the limit lines up")
            return RolePlan(role, m.name, "saturation", q, expr=plain, notes=tuple(notes))
        if hexpr is None:
            hexpr = summed(plain, J) if J else plain
        return RolePlan(role, m.name, "saturation", q, expr=hexpr, notes=tuple(notes))

    if role == "concurrency":
        if m.type not in ("gauge", None):
            notes.append(f"{m.name} is a {m.type}, not a gauge of requests in flight")
        expr = hexpr or summed(sel(m.name, mt), J)
        return RolePlan(role, m.name, "concurrency", q, expr=expr, notes=tuple(notes))

    # USE errors (and anything else): a rate of events, or the gauge as reported
    if hexpr is None:
        inner = f"rate({sel(m.name, mt)}[{RI}])" if m.type != "gauge" else sel(m.name, mt)
        hexpr = summed(inner, J) if J else inner
    return RolePlan(role, m.name, "errors", q, expr=hexpr, notes=tuple(notes))


def natural_bound(lo: float, hi: float, expr: str) -> float | None:
    """The natural upper bound of a utilization whose values span [lo, hi]: 1 for a share, 100
    for a percentage (by its expression), None when the data do not agree with either."""
    if lo < -0.01:
        return None
    if hi <= 1.05:
        return 1.0
    if hi <= 105 and ("percent" in expr or "100" in expr):
        return 100.0
    return None


def littles_selectors(
    infos: Mapping[str, MetricInfo], matchers: Mapping[str, str]
) -> dict[str, str]:
    """check_littles_law's role arguments for a Little's law binding's metrics."""
    return {r: sel(infos[r].name, matchers) for r in BINDING_ROLES["littles_law"]}


def _assumed(role: str) -> str:
    return {
        "rate": "counter",
        "arrival_rate": "counter",
        "errors": "counter",
        "concurrency": "gauge",
        "utilization": "gauge",
        "saturation": "gauge",
    }.get(role, "value")


def error_ratio(num: FetchResult, den: FetchResult, step_ms: int) -> tuple[FetchResult, list[str]]:
    """errors / requests per member and step with a Wilson interval (lo/hi columns).

    Counts are the mean per-second rate times the step (`n = rate x step`), so the interval is the
    sampling uncertainty of a share of that many requests. A step with no requests has no share
    (left out, never 0). A member whose error series is absent has had no errors: error counters
    usually appear only after the first one."""
    step_s = step_ms / 1000

    def by_labels(r: FetchResult) -> tuple[dict[str, str], dict[str, str]]:
        labels = {row["series_id"]: row["labels"] for row in r.series.to_pylist()}
        return labels, {v: k for k, v in labels.items()}

    num_labels, num_ids = by_labels(num)
    den_labels, _ = by_labels(den)
    num_vals: dict[tuple[str, int], float] = {}
    for row in num.buckets.to_pylist():
        if row["avg"] is not None and math.isfinite(row["avg"]):
            num_vals[(num_labels[row["series_id"]], row["ts_ms"])] = row["avg"]
    cols: dict[str, list] = {c: [] for c in ("ts_ms", "series_id", "avg", "min", "max", "count")}
    cols["lo"], cols["hi"] = [], []
    absent: set[str] = set()
    clipped = 0
    for row in den.buckets.to_pylist():
        rate = row["avg"]
        if rate is None or not math.isfinite(rate) or rate <= 0:
            continue
        lab = den_labels[row["series_id"]]
        n = rate * step_s
        if lab not in num_ids:
            absent.add(lab)
        k = num_vals.get((lab, row["ts_ms"]), 0.0)
        if k < 0:
            k = 0.0
        k *= step_s
        if k > n:
            clipped += 1
            k = n
        p = k / n
        lo, hi = wilson(k, n)
        lo, hi = min(lo, p), max(hi, p)  # float noise at p = 0 or 1
        for c, v in (
            ("ts_ms", row["ts_ms"]), ("series_id", row["series_id"]), ("avg", p), ("min", p),
            ("max", p), ("count", row["count"]), ("lo", lo), ("hi", hi),
        ):  # fmt: skip
            cols[c].append(v)
    schema = BUCKET_SCHEMA.append(pa.field("lo", pa.float64())).append(pa.field("hi", pa.float64()))
    notes: list[str] = []
    if absent:
        notes.append(
            f"{len(absent)} member(s) report no error series: counted as 0 errors (error "
            "counters usually appear only after the first error)"
        )
    if clipped:
        notes.append(
            f"{clipped} step(s) had more errors than requests (rates sampled apart): clipped to 1"
        )
    if not cols["ts_ms"]:
        notes.append("no step had any requests: there is no error share to draw")
    used = set(cols["series_id"])
    series = den.series.filter(pa.array([s in used for s in den.series["series_id"].to_pylist()]))
    return FetchResult(pa.table(cols, schema=schema), series), notes
