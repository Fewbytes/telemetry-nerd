"""The heatmap of a windowless sample dataset: a rows x columns matrix the caller supplied.

The colour scale is linear between the matrix minimum and maximum, and `legend` says so in words,
so the picture is never read without its mapping (graphing guide: honest scales)."""

from __future__ import annotations

ROW_LABEL_PREFIX = "r"


def sample_heatmap_payload(
    panel, meta, columns: dict[str, list[float | None]], row_labels: list[str] | None
) -> dict:
    """Row and column labels, cell values (row-major, None = missing), the colour scale's min and
    max, and the sentence that states the colour mapping."""
    labels = list(columns)
    row_count = len(columns[labels[0]])
    rows = row_labels or [f"{ROW_LABEL_PREFIX}{r}" for r in range(row_count)]
    cells = [[columns[c][r] for c in labels] for r in range(row_count)]
    drawn = [v for row in cells for v in row if v is not None]
    if not drawn:
        raise ValueError("every cell of the matrix is missing: nothing to draw")
    lo, hi = min(drawn), max(drawn)
    missing = row_count * len(labels) - len(drawn)
    legend = f"colour: linear from {lo:g} (darkest) to {hi:g} (lightest)"
    if missing:
        legend += f"; blank: {missing} missing cells"
    return {
        "kind": "sample_heatmap",
        "panel": panel.to_dict(),
        "dataset": meta.to_dict(),
        "caveats": [],
        "effective_step_ms": meta.step_ms,  # 0: no time axis
        "row_labels": rows,
        "column_labels": labels,
        "cells": cells,
        "min": lo,
        "max": hi,
        "legend": f"{row_count} rows x {len(labels)} columns; {legend}",
    }
