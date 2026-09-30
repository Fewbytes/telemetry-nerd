# src/telemetry_nerd/charts/spec.py
"""Declarative chart spec + validator. Rules are errors, not taste (spec §6.3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LINE_SERIES_BUDGET = 5


class Layer(BaseModel):
    mark: Literal["line+envelope"]
    data: str


class YAxis(BaseModel):
    range_mode: Literal["data", "reference", "semantic"] = "data"
    unit: str | None = None
    label: str | None = None


class ChartSpec(BaseModel):
    layers: list[Layer] = Field(min_length=1)
    y: YAxis = Field(default_factory=YAxis)


class ValidationIssue(BaseModel):
    rule: str
    message: str
    severity: Literal["error", "warning"]


def auto_spec(dataset_id: str) -> ChartSpec:
    # M1 has no catalog: no unit, no reference range. The UI labels "data" mode honestly.
    return ChartSpec(layers=[Layer(mark="line+envelope", data=dataset_id)])


def validate(spec: ChartSpec, series_counts: dict[str, int]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for layer in spec.layers:
        n = series_counts.get(layer.data, 0)
        if layer.mark == "line+envelope" and n > LINE_SERIES_BUDGET:
            issues.append(
                ValidationIssue(
                    rule="series_budget",
                    severity="error",
                    message=(
                        f"{layer.data} has {n} series; line charts allow at most "
                        f"{LINE_SERIES_BUDGET}; aggregate across series (e.g. sum by / avg by "
                        "a coarser label) or filter to the series that answer the question."
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
