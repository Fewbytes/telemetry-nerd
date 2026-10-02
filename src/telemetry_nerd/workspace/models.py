"""Typed reasoning objects (spec §3.3). Validation lives here; existence checks in the service."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from telemetry_nerd.analysis.exprkind import min_samples
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


class Hypothesis(_Strict):
    id: str
    statement: str = Field(min_length=1)
    status: HypothesisStatus = "proposed"
    author: str
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    created_at_ms: int
    updated_at_ms: int


class FindingIn(_Strict):
    claim: str = Field(min_length=1)
    scope: Scope
    evidence: list[EvidenceRef] = Field(min_length=1)
    caveats: list[str] = Field(default_factory=list)
    hypothesis: str | None = None
    stance: Literal["for", "against"] | None = None
    answers_panel: str | None = None

    @model_validator(mode="after")
    def _stance(self) -> FindingIn:
        if (self.hypothesis is None) != (self.stance is None):
            raise ValueError("hypothesis and stance must be given together")
        return self


Verdict = Literal["accepted", "rejected", "needs-more"]


class EvidenceFlag(_Strict):
    """An uncertainty flag the server derived for one evidence item (spec §5.3)."""

    evidence: int  # index into Finding.evidence
    flag: Literal["uncertainty_unknown", "input_uncertainty_unknown", "uncertainty_not_propagated"]
    message: str


class Finding(FindingIn):
    id: str
    author: str
    created_at_ms: int
    verdict: Verdict | None = None
    verdict_comment: str | None = None
    evidence_flags: list[EvidenceFlag] = Field(default_factory=list)


class MetricSuggestion(_Strict):
    name: str = Field(min_length=1)
    type: Literal["counter", "gauge", "histogram", "summary"]
    labels: list[str] = Field(default_factory=list)


class GapIn(_Strict):
    missing_signal: str = Field(min_length=1)
    needed_for: str = Field(min_length=1)
    suggestion: MetricSuggestion


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
