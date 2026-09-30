"""Typed reasoning objects (spec §3.3). Validation lives here; existence checks in the service."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from telemetry_nerd.model.time import parse_duration

Finite = Annotated[float, Field(allow_inf_nan=False)]


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
    method: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _uncertainty(self) -> StatisticRef:
        if self.exact and self.interval is not None:
            raise ValueError("contradictory: exact=true cannot be combined with an interval")
        if self.exact and not float(self.value).is_integer():
            raise ValueError("exact=true is only for integral quantities such as counts")
        if self.interval is None and not self.exact:
            raise ValueError(
                "no_uncertainty: a statistic needs an interval [lo, hi]; "
                "set exact=true only for exact quantities such as counts"
            )
        if self.interval is not None and self.interval[0] > self.interval[1]:
            raise ValueError("interval must be [lo, hi] with lo <= hi")
        return self


EvidenceRef = Annotated[PanelRef | AnnotationRef | StatisticRef, Field(discriminator="kind")]

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


class Finding(FindingIn):
    id: str
    author: str
    created_at_ms: int
    verdict: Verdict | None = None
    verdict_comment: str | None = None


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
