# src/telemetry_nerd/charts/spec.py
"""Declarative chart spec + validator. Rules are errors, not taste (spec §6.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from telemetry_nerd.charts.units import infer_unit

LINE_SERIES_BUDGET = 5


class Layer(BaseModel):
    mark: Literal["line+envelope"]
    data: str


class YAxis(BaseModel):
    range_mode: Literal["data", "reference", "semantic"] = "data"
    unit: str | None = None
    # Where `unit` came from: an explicit caller value, or a suffix hint from
    # the metric name. None means an unknown unit (UI labels that honestly).
    unit_provenance: str | None = None
    label: str | None = None


class ChartSpec(BaseModel):
    layers: list[Layer] = Field(min_length=1)
    y: YAxis = Field(default_factory=YAxis)


class ValidationIssue(BaseModel):
    rule: str
    message: str
    severity: Literal["error", "warning"]


def auto_spec(
    dataset_id: str,
    expr: str | None = None,
    unit: str | None = None,
    unit_provenance: str | None = None,
) -> ChartSpec:
    """No metric catalog yet (M3): units are inferred from metric-name suffixes.

    An explicit `unit` always wins over inference, recorded with the given
    provenance (e.g. "provided by claude"); without one, a consistent
    Prometheus suffix across the expression is recorded with its provenance.
    """
    y = YAxis()
    if unit:
        y.unit = unit
        y.unit_provenance = unit_provenance
    elif expr is not None and (inferred := infer_unit(expr)):
        y.unit = inferred
        y.unit_provenance = "inferred from metric name"
    return ChartSpec(layers=[Layer(mark="line+envelope", data=dataset_id)], y=y)


def validate(spec: ChartSpec, series_counts: dict[str, int]) -> list[ValidationIssue]:
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
        elif layer.mark == "line+envelope" and layer.data not in datasets:
            datasets.append(layer.data)
    total = sum(series_counts[d] for d in datasets)
    if total > LINE_SERIES_BUDGET:
        issues.append(
            ValidationIssue(
                rule="series_budget",
                severity="error",
                message=(
                    f"chart has {total} line series across {', '.join(datasets)}; line charts "
                    f"allow at most {LINE_SERIES_BUDGET}; aggregate across series (e.g. sum by / "
                    "avg by a coarser label) or filter to the series that answer the question."
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
