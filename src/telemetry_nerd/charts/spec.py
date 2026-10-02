# src/telemetry_nerd/charts/spec.py
"""Declarative chart spec + validator. Rules are errors, not taste (spec §6.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from telemetry_nerd.catalog.rules import facts_from_name
from telemetry_nerd.charts.dataview import SignalViews
from telemetry_nerd.charts.units import Lookup, infer_unit_with_provenance
from telemetry_nerd.charts.yview import MAX_SUGGESTIONS, YView
from telemetry_nerd.model.distribution import DEFAULT_QUANTILES, QUANTILE_CHOICES

LINE_SERIES_BUDGET = 5
Mark = Literal[
    "line+envelope",
    "heatmap",
    "percentiles",
    "histogram",
    "ecdf",
    "quantile_curve",
    "ccdf",
    "spectrum",
    "spectrogram",
    "spc",
    "seasonal",
    "fleet",
]
SPECTRAL_MARKS = {"spectrum", "spectrogram"}
WINDOW_MARKS = {"histogram", "ecdf", "quantile_curve", "ccdf"}
DISTRIBUTION_MARKS = {"heatmap", "percentiles", *WINDOW_MARKS}
MAX_QUANTILES = 4
FACET_BUDGET = 12
MAX_WINDOWS = 4


class Window(BaseModel):
    start_ms: int
    end_ms: int
    label: str = ""


class Layer(BaseModel):
    mark: Mark
    data: str
    windows: list[Window] = Field(default_factory=list)  # histogram/ecdf: windows compared
    color: Literal["count", "density"] = "count"  # heatmap colour
    # percentiles: which bands (also the initial chips of a heatmap's percentile view)
    quantiles: list[float] = Field(default_factory=lambda: list(DEFAULT_QUANTILES))
    role: Literal["main", "context"] = "main"  # context: the raw series behind a filtered panel
    segment_ms: int | None = None  # spectrogram window (explicit in provenance)
    overlap: float | None = Field(default=None, ge=0, lt=0.95)
    min_period_ms: int | None = None
    max_period_ms: int | None = None
    seasonal: dict | None = None  # compare_seasonal config: tz, exclude, reference datasets
    spc: dict | None = None  # analyze reference baseline: scheme, tz, reference datasets
    fleet: dict | None = None  # fleet config: by, scale, normalise


class YLimit(BaseModel):
    """A line drawn on a panel from catalog context (bead 2as.15): a hard limit (`bounded_by`),
    a threshold (`threshold_by` or a `thresholds` claim) or a reference series (`same_quantity`).

    A metric target has its own dataset over the panel's window and step; a constant threshold has
    only `value`. Every line says where it came from: no unexplained numbers."""

    metric: str
    dataset: str | None = None  # time dataset of the target; None for a constant
    hi: float  # the largest value on the line: what the y range must include for a limit
    basis: str = "bounded_by"  # how the context was derived, shown on hover
    kind: Literal["limit", "threshold", "reference"] = "limit"
    label: str | None = None
    origin: str | None = None  # pack / claude / user / stats ...
    confidence: float | None = None
    tone: Literal["bad", "warn", "info"] | None = None  # thresholds
    value: float | None = None  # a constant threshold


class YReframe(BaseModel):
    """A proposed way to show this metric so the picture carries its own context. Never applied
    silently: accepting one creates a new panel and leaves this one as it is."""

    title: str
    reason: str
    basis: str
    expr: str  # the replacement expression over the same window and step
    kind: Literal["substitute", "percent_of_limit", "headroom"]
    unit: str | None = None


class YProfile(BaseModel):
    """Operating range (robust, long window); filled by the T1 operating profile (2as.7)."""

    lo: float
    hi: float
    label: str = "normal range"


class YContext(BaseModel):
    """Catalog-derived inputs to the y range. A reference exists when a limit or profile does."""

    natural_lo: float | None = None
    natural_hi: float | None = None
    bounds: str | None = None  # the catalog claim the natural bounds came from
    bounds_origin: str | None = None
    limit: YLimit | None = None  # the strongest hard limit: it is part of the y range
    lines: list[YLimit] = Field(default_factory=list)  # every context line, limit included
    reframes: list[YReframe] = Field(default_factory=list)
    profile: YProfile | None = None
    notes: list[str] = Field(default_factory=list)  # honest gaps, shown to the user

    @property
    def has_reference(self) -> bool:
        return self.limit is not None or self.profile is not None


class YAxis(BaseModel):
    range_mode: Literal["data", "reference", "semantic"] = "data"
    unit: str | None = None
    # Where `unit` came from: an explicit caller value, or a suffix hint from
    # the metric name. None means an unknown unit (UI labels that honestly).
    unit_provenance: str | None = None
    label: str | None = None
    views: list[YView] = Field(
        default_factory=list, max_length=MAX_SUGGESTIONS
    )  # Claude's suggestions
    selected: YView | None = None  # the user's pick; None = auto
    context: YContext | None = None


class Reference(BaseModel):
    mode: Literal["previous", "week"]
    label: str
    start_ms: int
    end_ms: int
    shift_ms: int
    series: str  # time dataset over the reference window (same expr, step, source)
    dist: str | None = None  # distribution over the reference window (histogram-backed panels)
    dist_current: str | None = None  # distribution over the panel window


class Marginal(BaseModel):
    reference: Literal["previous", "week"]
    author: Literal["claude", "user"] = "user"
    reason: str | None = Field(default=None, max_length=160)


class Overlays(BaseModel):
    """Reference layers drawn under/over the data (bead 2as.11). Each is drawn only if it exists."""

    normal: bool = True  # seasonal normal band from the operating profile
    limit: bool = True  # physical limit line from a bounded_by relation
    ghost: bool = False  # the same window last week (costs a source fetch, so opt-in)


class AutoForm(BaseModel):
    """The panel shows a different form of the signal than the dataset it was asked to show,
    chosen from what the catalog knows (bead 2as.14). The original dataset is untouched."""

    transform: Literal["rate", "reframe"]
    source_dataset: str  # what was asked for
    reason: str


class ChartSpec(BaseModel):
    layers: list[Layer] = Field(min_length=1)
    y: YAxis = Field(default_factory=YAxis)
    references: dict[str, Reference] = Field(default_factory=dict)
    marginal: Marginal | None = None
    signal: SignalViews | None = None  # filtered/raw data views (4ok.9)
    overlays: Overlays = Field(default_factory=Overlays)
    auto: AutoForm | None = None


class ValidationIssue(BaseModel):
    rule: str
    message: str
    severity: Literal["error", "warning"]


def auto_spec(
    dataset_id: str,
    expr: str | None = None,
    unit: str | None = None,
    unit_provenance: str | None = None,
    representation: str = "bucket_agg",
    lookup: Lookup = facts_from_name,
) -> ChartSpec:
    """Units come from the metric catalog via `lookup` (rule-only name facts by default).

    An explicit `unit` always wins, recorded with the given provenance (e.g. "provided by
    claude"); without one, the unit every metric in the expression agrees on is used, with the
    origin of the claims behind it (user, claude, source metadata, name rule, ...).
    """
    y = YAxis()
    if unit:
        y.unit = unit
        y.unit_provenance = unit_provenance
    elif expr is not None:
        inferred, why = infer_unit_with_provenance(expr, lookup)
        if inferred:
            y.unit = inferred
            y.unit_provenance = why
    mark = "heatmap" if representation == "distribution" else "line+envelope"
    return ChartSpec(layers=[Layer(mark=mark, data=dataset_id)], y=y)


def validate(
    spec: ChartSpec,
    series_counts: dict[str, int],
    representations: dict[str, str] | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    datasets: list[str] = []
    for layer in spec.layers:
        if layer.data not in series_counts:
            if not any(i.rule == "unknown_dataset" and layer.data in i.message for i in issues):
                issues.append(
                    ValidationIssue(
                        rule="unknown_dataset",
                        severity="error",
                        message=f"dataset {layer.data} is not known; cannot count its series",
                    )
                )
        elif (
            layer.role == "main"
            and layer.mark in ("line+envelope", "spectrum", "spc")
            and layer.data not in datasets
        ):
            datasets.append(layer.data)
    reps = representations or {}
    for layer in spec.layers:
        rep = reps.get(layer.data)
        if rep is not None and (layer.mark in DISTRIBUTION_MARKS) != (rep == "distribution"):
            issues.append(ValidationIssue(
                rule="mark_representation", severity="error",
                message=(
                    f"{layer.mark} cannot draw {layer.data} ({rep}): distributions "
                    "(query_distribution) are drawn as heatmap, percentiles, histogram, ecdf, "
                    "quantile_curve or ccdf; "
                    "series datasets as lines"
                ),
            ))  # fmt: skip
        if layer.mark == "percentiles" and (
            not 1 <= len(layer.quantiles) <= MAX_QUANTILES
            or any(q not in QUANTILE_CHOICES for q in layer.quantiles)
        ):
            issues.append(ValidationIssue(
                rule="quantiles", severity="error",
                message=(
                    f"percentiles takes 1 to {MAX_QUANTILES} of "
                    f"{', '.join(f'{q:g}' for q in QUANTILE_CHOICES)}"
                ),
            ))  # fmt: skip
        if layer.mark in WINDOW_MARKS and not 1 <= len(layer.windows) <= MAX_WINDOWS:
            issues.append(ValidationIssue(
                rule="windows", severity="error",
                message=f"{layer.mark} needs 1 to {MAX_WINDOWS} time windows to compare",
            ))  # fmt: skip
        if layer.mark in DISTRIBUTION_MARKS and series_counts.get(layer.data, 0) > FACET_BUDGET:
            issues.append(ValidationIssue(
                rule="series_budget", severity="error",
                message=(
                    f"{series_counts[layer.data]} series would be {series_counts[layer.data]} "
                    f"small multiples; at most {FACET_BUDGET}: group by fewer labels (by=[...])"
                ),
            ))  # fmt: skip
    total = sum(series_counts[d] for d in datasets)
    if total > LINE_SERIES_BUDGET:
        issues.append(
            ValidationIssue(
                rule="series_budget",
                severity="error",
                message=(
                    f"chart has {total} line series across {', '.join(datasets)}; line charts "
                    f"allow at most {LINE_SERIES_BUDGET}; aggregate across series (e.g. sum by / "
                    "avg by a coarser label) or filter to the series that answer the question; "
                    "for many members of one metric (pods, nodes) use fleet(dataset) and "
                    'show(dataset, question, mark="fleet"): group band + outliers.'
                ),
            )
        )
    if spec.y.unit is None:
        issues.append(
            ValidationIssue(
                rule="units",
                severity="warning",
                message="y-axis unit unknown; it will come from the metric catalog (M3)",
            )
        )
    return issues
