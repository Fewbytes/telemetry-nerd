"""Evidence discipline for findings and hypotheses (principles 6, 8, 13; spec §4.4, §5.4; qxp).

Server-side guardrails, pure over what the service hands in:

* **Claim scope against evidence.** A finding's claim may only name entities (a service, pod,
  job... value of an `ENTITY_LABELS` label the workspace has seen) that its cited evidence covers.
  What one evidence dataset covers is read from its series labels and, for labels aggregated
  away, from its own expression's matchers (`claim_scope.pinned_matchers`): `sum by
  (status_code) (rate(x{service_name="payment"}[1m]))` covers payment only, and a sum over all
  services covers none of them one by one (pooled). An expression that cannot be read leaves the
  coverage *undetermined*, said as such, never assumed.
* **Supported hypotheses** need a concrete subject (an entity or metric the workspace knows), a
  finding for them that the user has not rejected, and an alternative considered: another
  hypothesis refuted or inconclusive, or an explicit `alternatives_considered` note.
* **Sources of variation.** A cited statistic without `source` gets the one the op that emitted
  it gave (`DatasetStore.statistic_sources`), else is flagged `source_undetermined`; a finding
  whose evidence attributes no variation at all is flagged once. Undetermined is never upgraded:
  a cited source that contradicts the op's is refused (a downgrade to undetermined is kept,
  flagged), and one no op stands behind is flagged `source_unverified` (i6y5).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from telemetry_nerd.analysis.sources import CITE_RANK, UNDETERMINED
from telemetry_nerd.core.claim_scope import (
    Leaf,
    Matcher,
    Node,
    Num,
    Opaque,
    Unreadable,
    leaf_matchers,
    parse_expr,
    read_selector,
)
from telemetry_nerd.core.entity_ops import base_name

#: labels whose values name an entity a claim can be about (a service, a workload, a host)
ENTITY_LABELS = frozenset({
    "service_name", "service", "job", "app", "application", "namespace", "deployment",
    "statefulset", "daemonset", "pod", "container", "instance", "host", "hostname", "node",
    "cluster", "k8s_deployment_name", "k8s_pod_name", "k8s_namespace_name", "service_namespace",
})  # fmt: skip

#: label values that are ordinary words in a claim ("the default source"), never read as entities
_GENERIC = frozenset({"default", "true", "false", "none", "null", "unknown", "all", "other",
                      "total", "yes", "no"})  # fmt: skip

_SUBJECT_TOKEN = re.compile(r"[A-Za-z_:][A-Za-z0-9_:.]*")


def _variants(value: str) -> list[str]:
    out = {value, value.replace("-", " "), value.replace("-", ""), value.replace("-", "_")}
    return sorted(out, key=len, reverse=True)


def _nameable(value: str) -> bool:
    return (
        len(value) >= 2
        and value.lower() not in _GENERIC
        and not value.replace(".", "").replace(":", "").isdigit()
    )


def mentions(text: str, value: str) -> bool:
    """Does `text` name `value` as a word (payment, PaymentService, product-catalog / product
    catalog)? Letters, digits and _ around it make it part of another word."""
    alts = "|".join(re.escape(v) for v in _variants(value))
    rx = rf"(?<![A-Za-z0-9_])(?:{alts})(?:service)?(?![A-Za-z0-9_])"
    return re.search(rx, text, re.IGNORECASE) is not None


# --- what evidence covers -------------------------------------------------------------------


@dataclass(frozen=True)
class Cover:
    """What one evidence dataset covers of one entity label. `all` / `any`: a combined
    expression (q1p): `a / b` covers a value when both sides do, `a or b` when either does."""

    kind: Literal["values", "matchers", "pooled", "undetermined", "all", "any"]
    values: frozenset[str] = frozenset()
    matchers: tuple[Matcher, ...] = ()
    #: what shows that a pooled dataset's series hold only `values` (see `single_values`):
    #: witness datasets, or the label listings (`entities`) that found one value
    via: tuple[str, ...] = ()
    parts: tuple[Cover, ...] = ()
    why: str = ""  # undetermined: why the expression was not read

    def covers(self, value: str) -> bool | None:
        if self.kind == "values":
            return value in self.values
        if self.kind == "matchers":
            return all(m.matches(value) for m in self.matchers)
        if self.kind in ("all", "any"):
            got = [p.covers(value) for p in self.parts]
            decisive = self.kind == "any"  # any: one True decides; all: one False decides
            if decisive in got:
                return decisive
            return None if None in got else not decisive
        return None if self.kind == "undetermined" else False

    def describe(self, label: str) -> str:
        if self.kind in ("all", "any"):
            parts = list(dict.fromkeys(p.describe(label) for p in self.parts))
            return ("each side: " if self.kind == "all" else "any of: ") + " | ".join(parts)
        if self.kind == "values" and self.via:
            (v,) = self.values
            return (f'{label}="{v}" (pooled over its only {label} value, per '
                    f"{', '.join(self.via)})")  # fmt: skip
        if self.kind == "values":
            return ", ".join(f'{label}="{v}"' for v in sorted(self.values)[:6]) + (
                ", ..." if len(self.values) > 6 else ""
            )
        if self.kind == "matchers":
            return ", ".join(map(str, self.matchers))
        if self.kind == "pooled":
            return f"all {label} values pooled (none one by one)"
        return f"{label} unknown (expression not read" + (f": {self.why})" if self.why else ")")


def _combine(kind: Literal["all", "any"], parts: Sequence[Cover]) -> Cover:
    flat: list[Cover] = []
    for p in parts:
        flat += list(p.parts) if p.kind == kind else [p]
    flat = list(dict.fromkeys(flat))
    return flat[0] if len(flat) == 1 else Cover(kind, parts=tuple(flat))


Single = Mapping[str, tuple[str, Sequence[str]]]


def _tree_cover(node: Node, label: str, single: Callable[[Leaf], Single | None]) -> Cover | None:
    """What an expression tree covers of `label`; None for a number (restricts nothing)."""
    if isinstance(node, Num):
        return None
    if isinstance(node, Opaque):
        return Cover("undetermined", why=node.why)
    if isinstance(node, Leaf):
        ms = tuple(m for m in node.matchers if m.label == label)
        if ms:
            return Cover("matchers", matchers=ms)
        one = single(node)
        if one and label in one:
            v, via = one[label]
            return Cover("values", frozenset({v}), via=tuple(via))
        return Cover("pooled")
    left = _tree_cover(node.left, label, single)
    right = _tree_cover(node.right, label, single)
    if left is None or right is None:
        return left if right is None else right
    if node.op in ("or", ","):
        return _combine("any", [left, right])
    if node.op in ("unless", "and"):  # the left side's series, filtered
        return left
    if not node.matched(label):
        return Cover("undetermined", why=f"{label} is not matched across {node.op!r}")
    return _combine("all", [left, right])


def evidence_cover(
    expr: str | None,
    series: Sequence[Mapping[str, str]],
    readable: bool = True,
    single: Single | Callable[[str], Single | None] | None = None,
) -> dict[str, Cover]:
    """Per entity label, what a dataset with these series (labels) and expression covers.
    `readable`: the expression is the query (not a code output or a filter). The expression
    may combine selectors (`sum(rate(a[1m])) / sum(rate(b[1m]))`, `a or b`, q1p): each side is
    read and their coverage intersected (binary operators) or joined (`or`). `single`: label ->
    (the one value, witnesses) where the pooled series are known to hold a single value of the
    label (`single_values`), or a function giving that per selector of the expression: pooling
    over one value covers that value."""
    node: Node | None = None
    why = ""
    if readable and expr:
        try:
            node = parse_expr(expr)
        except Unreadable as e:
            why = str(e)

    def per_leaf(leaf: Leaf) -> Single | None:
        return single(leaf.expr) if callable(single) else single

    out: dict[str, Cover] = {}
    for label in ENTITY_LABELS:
        vals = {lb[label] for lb in series if label in lb}
        if vals:
            out[label] = Cover("values", frozenset(vals))
        elif node is not None:
            out[label] = _tree_cover(node, label, per_leaf) or Cover("pooled")
        else:
            out[label] = Cover("undetermined", why=why)
    return out


@dataclass(frozen=True)
class Fetched:
    """A dataset as `single_values` needs it: its query, where and when, its series' labels."""

    id: str
    expr: str
    source: str
    start_ms: int
    end_ms: int
    series: Sequence[Mapping[str, str]]
    #: the dataset's resolution: a witness window may fall short of its range by up to one step
    #: (a listing taken "now" ends a scrape before a range rounded up to the next step, ipqy)
    step_ms: int = 0


@dataclass(frozen=True)
class Listing:
    """The values of one label a source's index listed over a window (`entities`): a witness
    for `single_values` when it found exactly one (q1p)."""

    source: str
    label: str
    values: tuple[str, ...]
    truncated: bool
    start_ms: int
    end_ms: int
    metric: str | None = None  # listed over one metric's series; None: the whole source
    via: str = "entities"  # how to cite it


def single_values(
    pooled: Fetched, others: Iterable[Fetched], listings: Iterable[Listing] = ()
) -> dict[str, tuple[str, list[str]]]:
    """Entity labels a pooled dataset aggregated away whose pooled series hold exactly one value,
    read from witnesses: other datasets of the same source and the same metric, over at least the
    same time range, selected with no matcher the pooled query lacks (so they hold at least its
    series) and keeping the label on every series; and label listings (`listings`, from the
    source's index: `entities`) of the same source over a window holding the pooled range, of the
    whole source or of the same metric, complete (not truncated). When every witness shows the
    same single value, the pooled series hold that value only (`sum(x)` over x{service="checkout"}
    alone is about checkout; a source whose service label has one value in the window is about
    that service). "Holding the range" allows one step of the pooled dataset at either edge
    (`Fetched.step_ms`): a range rounded to its step may start or end a scrape interval past a
    window ending "now", and that edge bucket is one sample (ipqy). No witness, or two values:
    nothing (the pooled cover stays pooled).
    `others` must be datasets whose expr is the query that produced them."""
    read = read_selector(pooled.expr)
    if read.matchers is None:
        return {}
    names = {m.value for m in read.matchers if m.label == "__name__" and m.op == "="}
    if len(names) != 1 or any(m.label == "__name__" and m.op != "=" for m in read.matchers):
        return {}
    rest = {m for m in read.matchers if m.label != "__name__"}
    tol = max(pooled.step_ms, 0)
    lo, hi = pooled.start_ms + tol, pooled.end_ms - tol  # a witness window must hold [lo, hi]
    seen: dict[str, set[str]] = {}
    via: dict[str, list[str]] = {}
    for w in others:
        if w.id == pooled.id or w.source != pooled.source or not w.series:
            continue
        if w.start_ms > lo or w.end_ms < hi:
            continue
        wr = read_selector(w.expr)
        if wr.matchers is None:
            continue
        w_names = {m.value for m in wr.matchers if m.label == "__name__" and m.op == "="}
        w_rest = {m for m in wr.matchers if m.label != "__name__"}
        if w_names != names or len(w_names) != sum(m.label == "__name__" for m in wr.matchers):
            continue
        if not w_rest <= rest:
            continue
        for label in ENTITY_LABELS:
            if any(m.label == label for m in rest):
                continue
            vals = {lb.get(label) for lb in w.series}
            if None in vals:
                continue  # aggregated away (or absent on some series): says nothing
            seen.setdefault(label, set()).update(v for v in vals if v is not None)
            via.setdefault(label, []).append(w.id)
    (name,) = names
    for li in listings:
        if li.source != pooled.source or li.label not in ENTITY_LABELS or li.truncated:
            continue
        if not li.values or any(m.label == li.label for m in rest):
            continue
        if li.start_ms > lo or li.end_ms < hi:
            continue
        if li.metric is not None and base_name(li.metric) != base_name(name):
            continue
        seen.setdefault(li.label, set()).update(li.values)
        via.setdefault(li.label, []).append(li.via)
    return {
        label: (next(iter(vals)), sorted(dict.fromkeys(via[label])))
        for label, vals in seen.items()
        if len(vals) == 1
    }


def known_entities(
    series_by_dataset: Mapping[str, Iterable[Mapping[str, str]]], exprs: Mapping[str, str]
) -> dict[str, dict[str, set[str]]]:
    """label -> value -> datasets that hold it: every entity value the workspace has seen, in
    series labels or as an equality matcher of a dataset's expression."""
    out: dict[str, dict[str, set[str]]] = {}
    for did, series in series_by_dataset.items():
        for lb in series:
            for k, v in lb.items():
                if k in ENTITY_LABELS and isinstance(v, str) and _nameable(v):
                    out.setdefault(k, {}).setdefault(v, set()).add(did)
    for did, expr in exprs.items():
        for m in leaf_matchers(expr) if expr else []:
            if m.label in ENTITY_LABELS and m.op == "=" and _nameable(m.value):
                out.setdefault(m.label, {}).setdefault(m.value, set()).add(did)
    return out


def expr_metrics(expr: str | None) -> set[str]:
    """Metric names an expression reads (histogram members as their base name)."""
    return {base_name(m.value) for m in leaf_matchers(expr) if m.label == "__name__" and
            m.op == "="} if expr else set()  # fmt: skip


# --- claim scope ----------------------------------------------------------------------------


@dataclass
class ClaimScope:
    status: Literal["covered", "beyond_evidence", "undetermined"]
    named: list[str] = field(default_factory=list)
    not_covered: list[str] = field(default_factory=list)
    undetermined: list[str] = field(default_factory=list)
    message: str = ""
    hint: str = ""  # where the uncovered entities are (datasets to cite)

    def to_dict(self) -> dict:
        return {"status": self.status, "named": self.named, "not_covered": self.not_covered,
                "undetermined": self.undetermined, "message": self.message}  # fmt: skip


def check_claim(
    claim: str,
    covers: Mapping[str, Mapping[str, Cover]],
    known: Mapping[str, Mapping[str, set[str]]],
    selector_undetermined: str | None = None,
    metrics_of: Mapping[str, set[str]] | None = None,
) -> ClaimScope:
    """Entities the claim names that the evidence (`covers`: dataset -> label -> Cover) does not
    cover. A value seen under several labels is covered when any of them covers it.
    `selector_undetermined`: why scope.selector could not be read (the scope is then at best
    undetermined). `metrics_of`: dataset -> metric names (histogram members as their base); the
    hint then offers only datasets of a measure the cited evidence is about (q1p: another
    metric holding the entity is not coverage for this claim)."""
    by_value: dict[str, list[str]] = {}
    for label in sorted(known):
        for value in sorted(known[label]):
            if mentions(claim, value):
                by_value.setdefault(value, []).append(label)
    named, not_covered, undetermined, where = [], [], [], []
    witnessed: list[str] = []  # covered only through a single-value witness: said, with it
    for value, labels in by_value.items():
        verdicts = {
            label: [c[label].covers(value) for c in covers.values() if label in c]
            for label in labels
        }
        named += [f'{label}="{value}"' for label in labels]
        if any(True in v for v in verdicts.values()):
            direct = [(did, lb) for did, c in covers.items() for lb in labels
                      if lb in c and c[lb].covers(value) is True]  # fmt: skip
            if all(_witnessed(covers[did][lb]) for did, lb in direct):
                witnessed += [f"{did}: {covers[did][lb].describe(lb)}" for did, lb in direct]
            continue
        if any(None in v for v in verdicts.values()):
            undetermined.append(f'{labels[0]}="{value}"')
            continue
        not_covered.append(f'{labels[0]}="{value}"')
        holders = sorted({d for label in labels for d in known[label][value]} - set(covers))
        cited = {x for d in covers for x in (metrics_of or {}).get(d, set())}
        if metrics_of is not None and cited:
            same = [d for d in holders if metrics_of.get(d, set()) & cited]
            if holders and not same:
                where.append(f'no dataset of {", ".join(sorted(cited))} has {labels[0]}="{value}" '
                             f'({", ".join(holders)} {"has" if len(holders) == 1 else "have"} it '
                             "for another metric, which does not cover this claim): query it "
                             f"by {labels[0]}, or list the source's {labels[0]} values over the "
                             "range (entities: a single value covers pooled evidence)")  # fmt: skip
                continue
            holders = same
        if holders:
            where.append(f'{", ".join(holders)} {"has" if len(holders) == 1 else "have"} '
                         f'{labels[0]}="{value}"')  # fmt: skip
    named_labels = sorted({n.split("=", 1)[0] for n in named})
    cited = "; ".join(
        f"{did}: " + ", ".join(c[lb].describe(lb) for lb in named_labels if lb in c)
        for did, c in covers.items()
    )
    if not_covered:
        msg = (f"the claim names {', '.join(not_covered)}, which its evidence does not cover "
               f"(cited evidence covers: {cited or 'none of the named entities'})")  # fmt: skip
        return ClaimScope("beyond_evidence", named, not_covered, undetermined, msg,
                          "; ".join(where))  # fmt: skip
    parts = []
    if undetermined:
        parts.append(f"whether the evidence covers {', '.join(undetermined)} is not known "
                     "(its expression could not be read)")  # fmt: skip
    if selector_undetermined:
        parts.append(selector_undetermined.removeprefix("scope undetermined: "))
    if parts:
        return ClaimScope("undetermined", named, [], undetermined,
                          "scope undetermined: " + "; ".join(parts))  # fmt: skip
    if witnessed:
        return ClaimScope("covered", named, message="covered through a single-value witness: "
                          + "; ".join(dict.fromkeys(witnessed)))  # fmt: skip
    return ClaimScope("covered", named)


def _witnessed(c: Cover) -> bool:
    """Does this cover rest on a single-value witness (not on series labels or matchers)?"""
    if c.kind in ("all", "any"):
        return any(_witnessed(p) for p in c.parts)
    return c.kind == "values" and bool(c.via)


# --- hypotheses -----------------------------------------------------------------------------


def subjects(
    statement: str,
    known: Mapping[str, Mapping[str, set[str]]],
    metrics: Iterable[str],
    is_metric: Callable[[str], bool] = lambda _t: False,
    selector: str | None = None,
) -> list[str]:
    """Concrete subjects a hypothesis statement names: entity values the workspace has seen,
    metric names of its datasets, or (catalog lookup) metric names of its sources. `selector`:
    the hypothesis' scope selector; its entity equality matchers on values the workspace has
    seen, and its metric names known as above, count too."""
    out = [f'{label}="{v}"' for label in sorted(known) for v in sorted(known[label])
           if mentions(statement, v)]  # fmt: skip
    ms = set(metrics)
    for tok in dict.fromkeys(_SUBJECT_TOKEN.findall(statement)):
        tok = tok.rstrip(".:")
        if tok in ms or (("_" in tok or ":" in tok) and is_metric(tok)):
            out.append(tok)
    for m in leaf_matchers(selector) if selector else []:
        if m.op != "=":
            continue
        if m.label == "__name__" and (m.value in ms or is_metric(m.value)):
            out.append(m.value)
        elif m.value in known.get(m.label, {}):
            out.append(f'{m.label}="{m.value}"')
    return list(dict.fromkeys(out))


def support_problems(
    hypothesis_id: str,
    statement: str,
    subject: list[str],
    evidence_for: Sequence[str],
    rejected: set[str],
    others: Mapping[str, str],
    alternatives_considered: str | None,
) -> list[str]:
    """Why a hypothesis may not be marked supported yet ([] = it may). `others`: id -> status
    of every other hypothesis; `rejected`: findings the user rejected."""
    out = []
    if not subject:
        out.append(f"{hypothesis_id} names no concrete subject: a supported hypothesis says which "
                   "service, resource or metric it is about (a label value or metric the "
                   "workspace has queried). Record a sharper hypothesis naming it "
                   "(hypothesis_create); a vague one does not count as a ruled-out alternative")  # fmt: skip
    standing = [f for f in evidence_for if f not in rejected]
    if not standing:
        out.append(f"{hypothesis_id} has no finding for it" + (
            " the user has not rejected" if evidence_for else "") +
            ": record one with finding_create(hypothesis=..., stance='for')")  # fmt: skip
    ruled = [h for h, st in others.items() if st in ("refuted", "inconclusive")]
    if not ruled and not (alternatives_considered or "").strip():
        out.append("no alternative considered: record the competing explanation as a hypothesis "
                   "and refute it (or mark it inconclusive) with evidence, or pass "
                   "alternatives_considered saying which alternatives you ruled out and how")  # fmt: skip
    return out


def ruled_out_problems(
    hypothesis_id: str,
    status: str,
    evidence_for: Sequence[str],
    evidence_against: Sequence[str],
    rejected: set[str],
    reason: str | None,
    note: str | None,
) -> list[str]:
    """Why Claude may not mark a hypothesis refuted / inconclusive yet ([] = it may), the
    symmetric side of `support_problems` (aiy). Refuted: a finding against it the user has not
    rejected, or an explicit `reason`. Inconclusive: any standing linked finding, a `reason`, or
    a `note` saying why. A note never stands in for a refutation's reason: it is not checked
    and does not link the evidence it may cite."""
    if (reason or "").strip():
        return []
    against = [f for f in evidence_against if f not in rejected]
    if status == "refuted":
        if against:
            return []
        rej = " the user has not rejected" if evidence_against else ""
        return [(f"{hypothesis_id} has no finding against it{rej}: record the observation that "
                f'rules it out with finding_create(hypotheses=[{{"id": "{hypothesis_id}", '
                '"stance": "against"}], ...) (one finding may also back another hypothesis: '
                'add {"id": ..., "stance": "for"} to the same list), or pass reason saying what '
                "rules it out and citing the findings it rests on")]  # fmt: skip
    if status == "inconclusive":
        standing = [f for f in [*evidence_for, *evidence_against] if f not in rejected]
        if standing or (note or "").strip():
            return []
        return [(f"{hypothesis_id} has no finding linked and no reason: say why it cannot be "
                 "decided (reason or note), e.g. which signal is missing")]  # fmt: skip
    return []


# --- sources of variation -------------------------------------------------------------------

_UNTRACED = ("not traced to an op result, and it carries no source: what its variation is "
             "attributed to (common cause, special cause, measurement system) is undetermined")  # fmt: skip
_NONE_CITED = ("no cited statistic says what the variation is attributed to: if this finding "
               "reports a change or deviation, its source is undetermined (cite an op's evidence "
               "statistic, e.g. from analyze, compare_seasonal, fleet or binding_verdict)")  # fmt: skip


_UNVERIFIED = ("not traced to an op result: the source {s} was supplied with the citation and no "
               "op model stands behind it (principle 16), so it is shown as unverified; cite the "
               "op statistic that labels it, or source undetermined")  # fmt: skip


_DOWNGRADED = "cited as undetermined; the op that emitted it labelled it {ops}"
_DISAGREE = (
    "ops gave this statistic different sources ({ops}): source undetermined; cite the op's "
    "statistic as is"
)
_DERIVED = "source {s} taken from the op result that emitted this statistic"


def _flag(evidence: int, flag: str, source: str, message: str) -> dict:
    """One source flag of a finding (on its `evidence` item)."""
    return {"evidence": evidence, "flag": flag, "source": source, "message": message}


def _ops_label(found: set[str]) -> str:
    return ", ".join(sorted(s or "none" for s in found))


def derive_sources(
    evidence: Sequence[dict], lookup: Callable[[dict], set[str] | None]
) -> tuple[list[dict], list[dict]]:
    """(evidence with derived sources filled in, source flags). `lookup(statistic)`: the sources
    ops gave it ('' = none), or None when no op emitted it.

    The op's label is authoritative (i6y5; principle 16, spec §5.4): a cited `source` that
    differs from it is refused (ValueError naming the op's label), except a downgrade to
    `undetermined`, kept and flagged `source_downgraded` (it can only say less than the op's
    model; the op's label stays in the flag). Ops disagreeing make the source undetermined: any
    other cited label is refused. A label on a statistic no op labelled is kept but flagged
    `source_unverified`, never trusted as derived."""
    ev = [dict(e) for e in evidence]
    flags: list[dict] = []
    refused: list[str] = []
    for i, e in enumerate(ev):
        if e.get("kind") != "statistic":
            continue
        cited = e.get("source")
        found = lookup(e)
        if cited:
            if found is not None and len(found) == 1 and cited == next(iter(found)):
                continue  # as the op labelled it
            labelled = {s for s in found or () if s}
            if cited == UNDETERMINED:  # ops disagreeing: undetermined is their answer too
                if labelled and len(found or ()) == 1:
                    why = _DOWNGRADED.format(ops=_ops_label(labelled))
                    flags.append(_flag(i, "source_downgraded", UNDETERMINED, why))
                continue
            if not labelled:  # untraced, or an op that attributes no variation to it
                flags.append(_flag(i, "source_unverified", cited, _UNVERIFIED.format(s=cited)))
                continue
            what = f"evidence[{i}] ({e.get('dataset')} {e.get('name')!r})"
            if len(found or ()) > 1:
                refused.append(f"{what}: cited source {cited}, but ops gave this statistic "
                               f"different sources ({_ops_label(found or set())}), so its source "
                               "is undetermined; cite it with the value and method of the op "
                               "result that emitted it, or with source undetermined")  # fmt: skip
            else:
                refused.append(f"{what}: cited source {cited}, but the op that emitted it "
                               f"labelled it {_ops_label(labelled)} (method: "
                               f"{_short(e.get('method'))}); an op's label is a model's output "
                               "and is kept as is (undetermined is never upgraded, a label is "
                               "never swapped): cite it without source (taken from the op) or "
                               "with source undetermined")  # fmt: skip
            continue
        if found is None:
            if not e.get("exact"):  # an exact count reports no variation
                flags.append(_flag(i, "source_undetermined", UNDETERMINED, _UNTRACED))
            continue
        if len(found) > 1:
            why = _DISAGREE.format(ops=_ops_label(found))
            flags.append(_flag(i, "source_undetermined", UNDETERMINED, why))
        elif (s := next(iter(found))) != "":
            e["source"] = s
            flags.append(_flag(i, "source_derived", s, _DERIVED.format(s=s)))
    if refused:
        raise ValueError("source_relabelled: " + "; ".join(refused) + ".")
    data = [i for i, e in enumerate(ev) if e.get("kind") in ("panel", "statistic")]
    if data and not flags and not any(e.get("source") for e in ev):
        flags.append(_flag(data[0], "source_undetermined", UNDETERMINED, _NONE_CITED))
    return ev, flags


def _short(method: object, width: int = 80) -> str:
    text = " ".join(str(method or "").split())
    return text if len(text) <= width else text[: width - 1].rsplit(" ", 1)[0] + "…"


# --- citable op statistics (hk2r) -----------------------------------------------------------

#: params worth showing beside a citable statistic (where / when / how sure)
_CITABLE_PARAMS = ("window", "at", "p", "phase", "group")
MAX_CITABLE = 5


def citable_statistics(
    labelled: Sequence[dict], cited: Sequence[dict], limit: int = MAX_CITABLE
) -> list[dict]:
    """Labelled op statistics (`DatasetStore.labelled_statistics`) a finding could cite in place
    of the panels it rests on: not already cited, special cause first, at most `limit`, compact
    and still citable as is (the method shortened, params cut to where / when / p)."""
    have = {(e.get("dataset"), e.get("name"), e.get("value")) for e in cited
            if e.get("kind") == "statistic"}  # fmt: skip
    seen: set[tuple] = set()
    out: list[dict] = []
    for st in sorted(labelled, key=lambda s: CITE_RANK.get(s.get("source") or "", 9)):
        key = (st["dataset"], st["name"], st["value"])
        if key in have or key in seen:
            continue
        seen.add(key)
        params = {k: v for k, v in (st.get("params") or {}).items() if k in _CITABLE_PARAMS}
        out.append({**{k: v for k, v in st.items() if k != "params"},
                    "method": _short(st.get("method"), 100),
                    **({"params": params} if params else {})})  # fmt: skip
        if len(out) >= limit:
            break
    return out


def citable_hint(stats: Sequence[dict], undetermined: bool) -> str:
    """One line telling the caller to cite the op statistics instead of (or beside) panels."""
    dids = ", ".join(dict.fromkeys(s["dataset"] for s in stats))
    lead = ("this finding's source is undetermined: a panel or annotation carries no variation "
            "source" if undetermined else "the cited panels carry no variation source")  # fmt: skip
    return (f"{lead}, but ops labelled statistics of {dids} (citable_statistics, special cause "
            "first). For a change or deviation, cite the matching one in evidence as given "
            "(kind statistic, its source kept): record a new finding with it, or keep this one "
            "and say its source is undetermined.")  # fmt: skip
