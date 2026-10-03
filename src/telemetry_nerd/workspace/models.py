"""Typed reasoning objects (spec §3.3). Validation lives here; existence checks in the service."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from telemetry_nerd.analysis.exprkind import min_samples
from telemetry_nerd.analysis.sources import Source as VariationSource
from telemetry_nerd.model.time import parse_duration

Finite = Annotated[float, Field(allow_inf_nan=False)]
_PERCENTILE = re.compile(
    r"(?<![a-z0-9])(?:p|q|pct)(?P<pct>\d+(?:\.\d+)?)(?![0-9])"
    r"|(?<![a-z])(?P<median>median)(?![a-z])(?!\s+absolute)"
    r"|(?P<nth>\d+(?:\.\d+)?)(?:st|nd|rd|th)\s*percentile"
    r"|quantile|percentile",
    re.IGNORECASE,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeSpan(_Strict):
    start_ms: int
    end_ms: int

    @model_validator(mode="after")
    def _ordered(self) -> TimeSpan:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms")
        return self


class Scope(_Strict):
    source: str = Field(min_length=1)
    selector: str = Field(min_length=1)
    time_range: TimeSpan
    step: str
    aggregation: str = Field(min_length=1)
    baseline_range: TimeSpan | None = None

    @field_validator("step")
    @classmethod
    def _step(cls, v: str) -> str:
        return check_step(v)


def check_step(v: str) -> str:
    try:
        if parse_duration(v) <= 0:
            raise ValueError
    except ValueError as e:
        raise ValueError(f"step must be a positive duration like 30s or 1m, got {v!r}") from e
    return v


class PanelRef(_Strict):
    kind: Literal["panel"]
    panel: str


class AnnotationRef(_Strict):
    kind: Literal["annotation"]
    annotation: str


class StatisticRef(_Strict):
    kind: Literal["statistic"]
    dataset: str
    name: str = Field(min_length=1)
    value: Finite
    interval: tuple[Finite, Finite] | None = None
    exact: bool = False
    #: the value's uncertainty is not known (spec §5.3): citable, and the finding says so
    uncertainty_unknown: bool = False
    method: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)
    #: what the statistic's variation is attributed to (spec §5.4): the op's label, kept as is
    source: VariationSource | None = None

    @model_validator(mode="after")
    def _uncertainty(self) -> StatisticRef:
        stated = (self.interval is not None) + self.exact + self.uncertainty_unknown
        if self.exact and self.interval is not None:
            raise ValueError("contradictory: exact=true cannot be combined with an interval")
        if stated > 1:
            raise ValueError(
                "contradictory: uncertainty_unknown=true goes without interval and exact"
            )
        if self.exact and not float(self.value).is_integer():
            raise ValueError("exact=true is only for integral quantities such as counts")
        if stated == 0:
            raise ValueError(
                "no_uncertainty: a statistic states its uncertainty: an interval [lo, hi] "
                "(derive one: bootstrap, effective n, bucket bounds, Wilson), exact=true for "
                "exact quantities such as counts, or uncertainty_unknown=true when none can be "
                "derived (the finding then shows 'uncertainty unknown')"
            )
        if self.interval is not None and self.interval[0] > self.interval[1]:
            raise ValueError("interval must be [lo, hi] with lo <= hi")
        m = _PERCENTILE.search(self.name.strip())
        if m:
            if m.group("pct"):
                q = float(m.group("pct")) / 100
            elif m.group("nth"):
                q = float(m.group("nth")) / 100
            elif m.group("median"):
                q = 0.5
            else:
                q = self.params.get("q")
            if not isinstance(q, int | float) or not 0 < q < 1:
                raise ValueError("a quantile statistic needs params.q in (0, 1), e.g. 0.95")
            n = self.params.get("n")
            if isinstance(n, float) and n.is_integer():
                n = int(n)
            if not isinstance(n, int) or isinstance(n, bool):
                raise ValueError(
                    "percentile_without_n: a percentile needs params.n, the number of "
                    "observations behind it (e.g. histogram_count over the same window)"
                )
            need = min_samples(float(q))
            if n < need:
                raise ValueError(
                    f"percentile_not_meaningful: n={n} < {need} needed for q={q}; "
                    "report the count or the mean instead"
                )
        return self


class ClaimRef(_Strict):
    """Catalog claims as evidence: where origins disagree about a metric's field."""

    kind: Literal["claim"]
    source: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    field: str = Field(min_length=1)
    origins: list[str] = Field(min_length=2)
    note: str | None = None


EvidenceRef = Annotated[
    PanelRef | AnnotationRef | StatisticRef | ClaimRef, Field(discriminator="kind")
]

AnnotationKind = Literal["event", "region", "threshold", "band", "note"]


class AnnotationIn(_Strict):
    kind: AnnotationKind
    panel: str | None = None
    t_start_ms: int | None = None
    t_end_ms: int | None = None
    value: Finite | None = None
    value_hi: Finite | None = None
    label: str = ""
    links: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_rules(self) -> AnnotationIn:
        k = self.kind
        if k == "event" and self.t_start_ms is None:
            raise ValueError("event annotations need t_start_ms")
        if k == "region" and (
            self.t_start_ms is None or self.t_end_ms is None or self.t_end_ms <= self.t_start_ms
        ):
            raise ValueError("region annotations need t_start_ms < t_end_ms")
        if k == "threshold" and self.value is None:
            raise ValueError("threshold annotations need value")
        if k == "band" and (
            self.value is None or self.value_hi is None or self.value_hi <= self.value
        ):
            raise ValueError("band annotations need value < value_hi")
        if k == "note" and (self.panel is None or not self.label.strip()):
            raise ValueError("note annotations need a panel and a label")
        return self


class Annotation(AnnotationIn):
    id: str
    author: str
    created_at_ms: int
    deleted: bool = False


HypothesisStatus = Literal["proposed", "supported", "refuted", "inconclusive"]


class HypothesisScope(_Strict):
    """Where a hypothesis applies (qy7q): a selector over a time range (source, step and
    aggregation when known), or free text when the scope was given as prose. Text is stored as
    given and is not checkable; the structured form is read like a finding's scope."""

    text: str | None = None
    source: str | None = None
    selector: str | None = None
    time_range: TimeSpan | None = None
    step: str | None = None
    aggregation: str | None = None

    @model_validator(mode="after")
    def _either(self) -> HypothesisScope:
        structured = (self.source, self.selector, self.time_range, self.step, self.aggregation)
        if self.text is not None:
            if not self.text.strip():
                raise ValueError("scope text must not be empty")
            if any(v is not None for v in structured):
                raise ValueError("a scope is either text or {selector, start, end, ...}, not both")
            return self
        if not (self.selector or "").strip() or self.time_range is None:
            raise ValueError("a structured scope needs selector, start and end")
        if self.step is not None:
            check_step(self.step)
        return self


class Hypothesis(_Strict):
    id: str
    statement: str = Field(min_length=1)
    status: HypothesisStatus = "proposed"
    author: str
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    #: the competing explanations considered and why they were set aside (principle 13), when
    #: they are not recorded as refuted / inconclusive hypotheses of their own
    alternatives_considered: str | None = None
    #: why the hypothesis was refuted or left inconclusive when no finding against it says so
    #: (aiy): stated explicitly, never inferred from a note
    status_reason: str | None = None
    scope: HypothesisScope | None = None
    created_at_ms: int
    updated_at_ms: int


Stance = Literal["for", "against"]


class HypothesisLink(_Strict):
    """A finding's stance on one hypothesis (aiy): one finding may back h1 and count against h2."""

    id: str = Field(min_length=1)
    stance: Stance


def _legacy_link(data: object) -> object:
    """Findings stored (or sent) before aiy carry one `hypothesis` + `stance`: read them as a
    one-item `hypotheses` list. Both forms at once must agree (the single link is in the list)."""
    if not isinstance(data, dict) or ("hypothesis" not in data and "stance" not in data):
        return data
    d = dict(data)
    h, st = d.pop("hypothesis", None), d.pop("stance", None)
    if (h is None) != (st is None):
        raise ValueError("hypothesis and stance must be given together")
    if h is None:
        return d
    links = list(d.get("hypotheses") or [])
    same = [
        x for x in links if (x.get("id") if isinstance(x, dict) else getattr(x, "id", None)) == h
    ]
    if same:
        given = same[0].get("stance") if isinstance(same[0], dict) else same[0].stance
        if given != st:
            raise ValueError(f"hypothesis {h} is given twice with different stances")
        return d
    d["hypotheses"] = [{"id": h, "stance": st}, *links]
    return d


class FindingIn(_Strict):
    claim: str = Field(min_length=1)
    scope: Scope
    evidence: list[EvidenceRef] = Field(min_length=1)
    caveats: list[str] = Field(default_factory=list)
    #: the hypotheses this finding takes a stance on, one link each (aiy); the single
    #: `hypothesis` + `stance` form (and stored findings before aiy) read as a one-item list
    hypotheses: list[HypothesisLink] = Field(default_factory=list)
    answers_panel: str | None = None
    #: why the claim names entities its evidence does not cover; required for such a claim
    #: (the finding is then flagged beyond_evidence, never silently accepted)
    scope_note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data: object) -> object:
        return _legacy_link(data)

    @model_validator(mode="after")
    def _links(self) -> FindingIn:
        seen: dict[str, Stance] = {}
        for link in self.hypotheses:
            if seen.get(link.id, link.stance) != link.stance:
                raise ValueError(f"a finding cannot be both for and against {link.id}")
            seen[link.id] = link.stance
        if len(seen) != len(self.hypotheses):  # the same link twice: kept once
            self.hypotheses = [HypothesisLink(id=h, stance=st) for h, st in seen.items()]
        return self

    def stance_on(self, hypothesis_id: str) -> Stance | None:
        return next((x.stance for x in self.hypotheses if x.id == hypothesis_id), None)

    @property
    def sources(self) -> list[str]:
        """Variation sources of the cited statistics (spec §5.4), in citation order."""
        found = (getattr(e, "source", None) for e in self.evidence)
        return list(dict.fromkeys(s for s in found if s))


Verdict = Literal["accepted", "rejected", "needs-more"]


class EvidenceFlag(_Strict):
    """An uncertainty flag the server derived for one evidence item (spec §5.3)."""

    evidence: int  # index into Finding.evidence
    flag: Literal["uncertainty_unknown", "input_uncertainty_unknown", "uncertainty_not_propagated"]
    message: str


ScopeStatus = Literal["covered", "beyond_evidence", "undetermined"]


class ScopeCheck(_Strict):
    """What the server found about the claim's scope against its evidence (spec §4.4, qxp).

    covered: every entity the claim names (a service, pod, job... value seen in the workspace)
    is covered by the cited evidence; beyond_evidence: some are not (the finding carries a
    scope_note); undetermined: the evidence or scope.selector could not be read, so coverage is
    not established. Entities are written label="value"."""

    status: ScopeStatus
    named: list[str] = Field(default_factory=list)
    not_covered: list[str] = Field(default_factory=list)
    undetermined: list[str] = Field(default_factory=list)
    message: str = ""


class SourceFlag(_Strict):
    """Where a finding's source of variation (spec §5.4) came from, when not as cited."""

    evidence: int  # index into Finding.evidence
    flag: Literal["source_derived", "source_undetermined"]
    source: VariationSource | None = None
    message: str


class Finding(FindingIn):
    id: str
    author: str
    created_at_ms: int
    verdict: Verdict | None = None
    verdict_comment: str | None = None
    evidence_flags: list[EvidenceFlag] = Field(default_factory=list)
    scope_check: ScopeCheck | None = None
    source_flags: list[SourceFlag] = Field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        """Cited variation sources, plus `undetermined` when the server flagged that no source
        could be established for part of the evidence (never upgraded)."""
        out = super().sources
        if any(f.flag == "source_undetermined" for f in self.source_flags) and (
            "undetermined" not in out
        ):
            out.append("undetermined")
        return out


class MetricSuggestion(_Strict):
    name: str = Field(min_length=1)
    type: Literal["counter", "gauge", "histogram", "summary"]
    labels: list[str] = Field(default_factory=list)


class GapIn(_Strict):
    missing_signal: str = Field(min_length=1)
    needed_for: str = Field(min_length=1)
    #: the metric to add, when one can be named (missing history or coverage names none)
    suggestion: MetricSuggestion | None = None


class Gap(GapIn):
    id: str
    author: str
    created_at_ms: int


class Message(_Strict):
    id: str
    thread: str
    author: str
    text: str = Field(min_length=1)
    created_at_ms: int


class Thread(_Strict):
    id: str
    anchor: str | None = None
    selection: TimeSpan | None = None
    author: str
    created_at_ms: int
    messages: list[Message] = Field(default_factory=list)


CodeStatus = Literal["running", "ok", "failed"]


class CodeOutput(_Strict):
    """A dataset a run stored via tn.put / tn.put_fit (lineage: node -> dataset)."""

    name: str  # output name in the run
    dataset: str
    representation: str
    rows: int
    caveats: list[str] = Field(default_factory=list)
    #: spec §5.3: every output is citable; kept so nodes stored before x2x still load
    evidence_ok: bool = True
    #: uncertainty status (no_uncertainty | input_uncertainty_unknown |
    #: uncertainty_not_propagated), None when the declared interval/exactness is the whole story
    uncertainty: str | None = None


class CodeIssue(_Strict):
    """An output the run left behind that was not ingested (see exchange.run.Issue)."""

    name: str
    code: str  # run_failed | uncommitted | partial_write | corrupt | invalid
    message: str


class CodeNode(_Strict):
    """A tier-2 run (spec §5.2): stored code, declared inputs, outcome and outputs.

    Created `running`; finished once (`ok` / `failed`) and never re-executed in place:
    a re-run is a new node with `rerun_of` set (nodes are immutable, §3.3)."""

    id: str
    code: str = Field(min_length=1)
    inputs: list[str] = Field(default_factory=list)
    status: CodeStatus = "running"
    author: str
    created_at_ms: int
    finished_at_ms: int | None = None
    timeout_s: float | None = None
    rerun_of: str | None = None
    #: kernel outcome: ok | error | timeout | crashed | not_run (never reached the kernel)
    exec_status: str | None = None
    duration_s: float | None = None
    stdout: str = ""
    stderr: str = ""
    result: str | None = None  # repr of the last expression, like a notebook's Out[]
    error: str | None = None
    traceback: str | None = None
    restarted: bool = False  # the kernel restarted: in-memory state from earlier runs is gone
    truncated: bool = False  # stdout/stderr were cut (head + tail kept)
    outputs: list[CodeOutput] = Field(default_factory=list)
    issues: list[CodeIssue] = Field(default_factory=list)


class GroupRole(_Strict):
    """One role of a panel group (bead czt.3): its panel, or the gap where its signal is missing."""

    role: str
    metric: str | None = None
    panel: str | None = None
    #: how the role is drawn: rate | error_ratio | distribution | mean | utilization | saturation |
    #: concurrency | errors | value | littles (the model check)
    form: str | None = None
    #: model: a panel that checks the binding's model (Little's law, czt.2)
    view: Literal["lines", "fleet", "heatmap", "model", "gap", "error"] = "gap"
    members: int | None = None  # series behind the panel (fleet when many)
    notes: list[str] = Field(default_factory=list)
    #: unfilled role: what to instrument (and the Gap object, for a confirmed binding)
    suggestion: MetricSuggestion | None = None
    why: str | None = None
    gap: str | None = None
    error: str | None = None
    #: binding_verdict (czt.4): {status, direction, pattern, text, at_capacity, onset?}
    verdict: dict | None = None


class PanelGroup(_Strict):
    """Panels drawn together for one model binding (USE / RED / Little's law): one time range and
    step, linked selection; closing the group closes every member panel."""

    id: str
    kind: str
    key: str
    source: str
    author: str
    created_at_ms: int
    start_ms: int
    end_ms: int
    step_ms: int
    #: where the roles came from: "binding" (a confirmed catalog binding) or "suggestion"
    basis: Literal["binding", "suggestion"]
    binding_origin: str | None = None
    suggestion: str | None = None
    join_on: list[str] = Field(default_factory=list)
    matchers: dict[str, str] = Field(default_factory=dict)
    error_matcher: str | None = None
    roles: list[GroupRole]
    notes: list[str] = Field(default_factory=list)
    closed: bool = False
    reframed_from: str | None = None
    #: binding_verdict (czt.4): {text, first, reference, alpha, at_ms}
    verdict: dict | None = None
