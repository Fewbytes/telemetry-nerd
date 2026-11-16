"""The bar chart of a windowless sample dataset: caller columns, one value per row label.

One column is a plain bar chart (sorted descending); several columns are a grouped chart, one bar
series per column in the caller's label order so the same label sits at the same place in each."""

from __future__ import annotations


def sample_bar_payload(
    panel, meta, columns: dict[str, list[float | None]], row_labels: list[str]
) -> dict:
    """A `kind: "bar"` payload: per column its (label, value) rows, None = missing (no value)."""
    layer = panel.spec["layers"][0]
    series = [
        {
            "label": name,
            "rows": [{"label": lab, "value": v} for lab, v in zip(row_labels, columns[name])],
        }
        for name in layer["columns"]
    ]
    return {
        "kind": "bar",
        "panel": panel.to_dict(),
        "dataset": meta.to_dict(),
        "caveats": [],
        "effective_step_ms": meta.step_ms,  # 0: no time axis
        "order": "descending" if len(series) == 1 else "given",
        "series": series,
    }
