"""The histogram of a windowless sample dataset (supplied columns, no time axis).

Bin edges are shared by every compared column, so the bars line up; n and missing counts and
the bin rule are stated in `text` (graphing guide: honest sizes)."""

from __future__ import annotations

import numpy as np

MAX_SAMPLE_BINS = 200  # Freedman-Diaconis on a heavy-tailed column can ask for thousands
BIN_RULE = "Freedman-Diaconis"


def sample_histogram_payload(panel, meta, columns: dict[str, list[float | None]]) -> dict:
    """Per compared column: label, n (values drawn), missing (nulls supplied), counts over
    shared `edges`; plus the sentence that states n, missing and the bin rule."""
    layer = panel.spec["layers"][0]
    drawn = {
        label: np.array([v for v in columns[label] if v is not None], dtype=float)
        for label in layer["columns"]
    }
    pooled = np.concatenate(list(drawn.values()))
    if pooled.size == 0:
        raise ValueError("every value of the compared columns is missing: nothing to draw")
    edges = np.histogram_bin_edges(pooled, bins="fd")
    rule = f"{BIN_RULE} rule"
    if len(edges) - 1 > MAX_SAMPLE_BINS:
        edges = np.histogram_bin_edges(pooled, bins=MAX_SAMPLE_BINS)
        rule = f"{MAX_SAMPLE_BINS} equal-width bins (the {BIN_RULE} rule asked for more)"
    series = [
        {
            "label": label,
            "n": int(values.size),
            "missing": len(columns[label]) - int(values.size),
            "counts": np.histogram(values, bins=edges)[0].tolist(),
        }
        for label, values in drawn.items()
    ]
    text = "; ".join(f"{s['label']}: n={s['n']}, missing={s['missing']}" for s in series)
    return {
        "kind": "sample_histogram",
        "mark": layer["mark"],
        "panel": panel.to_dict(),
        "dataset": meta.to_dict(),
        "edges": edges.tolist(),
        "series": series,
        "bin_rule": rule,
        "text": f"{text}; {len(edges) - 1} bins, {rule}",
    }
