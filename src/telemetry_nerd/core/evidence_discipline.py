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
  whose evidence attributes no variation at all is flagged once. Undetermined is never upgraded.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from telemetry_nerd.core.claim_scope import Matcher, read_selector

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
    """What one evidence dataset covers of one entity label."""

    kind: Literal["values", "matchers", "pooled", "undetermined"]
    values: frozenset[str] = frozenset()
    matchers: tuple[Matcher, ...] = ()

    def covers(self, value: str) -> bool | None:
        if self.kind == "values":
            return value in self.values
        if self.kind == "matchers":
            return all(m.matches(value) for m in self.matchers)
        return None if self.kind == "undetermined" else False

    def describe(self, label: str) -> str:
        if self.kind == "values":
            return ", ".join(f'{label}="{v}"' for v in sorted(self.values)[:6]) + (
                ", ..." if len(self.values) > 6 else ""
            )
        if self.kind == "matchers":
            return ", ".join(map(str, self.matchers))
        if self.kind == "pooled":
            return f"all {label} values pooled (none one by one)"
        return f"{label} unknown (expression not read)"


def evidence_cover(
    expr: str | None, series: Sequence[Mapping[str, str]], readable: bool = True
) -> dict[str, Cover]:
    """Per entity label, what a dataset with these series (labels) and expression covers.
    `readable`: the expression is the query (not a code output or a filter)."""
    read = read_selector(expr) if readable and expr else None
    out: dict[str, Cover] = {}
    for label in ENTITY_LABELS:
        vals = {lb[label] for lb in series if label in lb}
        if vals:
            out[label] = Cover("values", frozenset(vals))
        elif read is not None and read.matchers is not None:
            ms = tuple(m for m in read.matchers if m.label == label)
            out[label] = Cover("matchers", matchers=ms) if ms else Cover("pooled")
        else:
            out[label] = Cover("undetermined")
    return out


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
        read = read_selector(expr) if expr else None
        for m in (read.matchers or []) if read else []:
            if m.label in ENTITY_LABELS and m.op == "=" and _nameable(m.value):
                out.setdefault(m.label, {}).setdefault(m.value, set()).add(did)
    return out


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
) -> ClaimScope:
    """Entities the claim names that the evidence (`covers`: dataset -> label -> Cover) does not
    cover. A value seen under several labels is covered when any of them covers it.
    `selector_undetermined`: why scope.selector could not be read (the scope is then at best
    undetermined)."""
    by_value: dict[str, list[str]] = {}
    for label in sorted(known):
        for value in sorted(known[label]):
            if mentions(claim, value):
                by_value.setdefault(value, []).append(label)
    named, not_covered, undetermined, where = [], [], [], []
    for value, labels in by_value.items():
        verdicts = {
            label: [c[label].covers(value) for c in covers.values() if label in c]
            for label in labels
        }
        named += [f'{label}="{value}"' for label in labels]
        if any(True in v for v in verdicts.values()):
            continue
        if any(None in v for v in verdicts.values()):
            undetermined.append(f'{labels[0]}="{value}"')
            continue
        not_covered.append(f'{labels[0]}="{value}"')
        holders = sorted({d for label in labels for d in known[label][value]} - set(covers))
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
    return ClaimScope("covered", named)


# --- hypotheses -----------------------------------------------------------------------------


def subjects(
    statement: str,
    known: Mapping[str, Mapping[str, set[str]]],
    metrics: Iterable[str],
    is_metric: Callable[[str], bool] = lambda _t: False,
) -> list[str]:
    """Concrete subjects a hypothesis statement names: entity values the workspace has seen,
    metric names of its datasets, or (catalog lookup) metric names of its sources."""
    out = [f'{label}="{v}"' for label in sorted(known) for v in sorted(known[label])
           if mentions(statement, v)]  # fmt: skip
    ms = set(metrics)
    for tok in dict.fromkeys(_SUBJECT_TOKEN.findall(statement)):
        tok = tok.rstrip(".:")
        if tok in ms or (("_" in tok or ":" in tok) and is_metric(tok)):
            out.append(tok)
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


# --- sources of variation -------------------------------------------------------------------

_UNTRACED = ("not traced to an op result, and it carries no source: what its variation is "
             "attributed to (common cause, special cause, measurement system) is undetermined")  # fmt: skip
_NONE_CITED = ("no cited statistic says what the variation is attributed to: if this finding "
               "reports a change or deviation, its source is undetermined (cite an op's evidence "
               "statistic, e.g. from analyze, compare_seasonal, fleet or binding_verdict)")  # fmt: skip


def derive_sources(
    evidence: Sequence[dict], lookup: Callable[[dict], set[str] | None]
) -> tuple[list[dict], list[dict]]:
    """(evidence with derived sources filled in, source flags). `lookup(statistic)`: the sources
    ops gave it ('' = none), or None when no op emitted it."""
    ev = [dict(e) for e in evidence]
    flags: list[dict] = []
    for i, e in enumerate(ev):
        if e.get("kind") != "statistic" or e.get("source"):
            continue
        found = lookup(e)
        if found is None:
            if not e.get("exact"):  # an exact count reports no variation
                flags.append({"evidence": i, "flag": "source_undetermined",
                              "source": "undetermined", "message": _UNTRACED})  # fmt: skip
            continue
        if len(found) > 1:
            names = ", ".join(sorted(s or "none" for s in found))
            flags.append({"evidence": i, "flag": "source_undetermined", "source": "undetermined",
                          "message": f"ops gave this statistic different sources ({names}): "
                          "source undetermined; cite the op's statistic as is"})  # fmt: skip
        elif (s := next(iter(found))) != "":
            e["source"] = s
            flags.append({"evidence": i, "flag": "source_derived", "source": s,
                          "message": f"source {s} taken from the op result that emitted this "
                          "statistic"})  # fmt: skip
    data = [i for i, e in enumerate(ev) if e.get("kind") in ("panel", "statistic")]
    if data and not flags and not any(e.get("source") for e in ev):
        flags.append({"evidence": data[0], "flag": "source_undetermined",
                      "source": "undetermined", "message": _NONE_CITED})  # fmt: skip
    return ev, flags
