"""Q-Q, quantile curve and CCDF of a windowless sample dataset (supplied columns, no time axis).

Each column becomes one `WindowHist` for the existing distribution renderer: one point-mass bucket
per distinct value, so every cumulative view is exact at each value (nothing is binned). The
column's n and missing count travel with it and are stated on the panel (graphing guide: honest
sizes)."""

from __future__ import annotations

from collections import Counter


def sample_cumulative_payload(panel, meta, columns: dict[str, list[float | None]]) -> dict:
    """A `kind: "histogram"` payload with one series whose windows are the compared columns."""
    layer = panel.spec["layers"][0]
    windows = []
    for label in layer["columns"]:
        counts = Counter(v for v in columns[label] if v is not None)
        if not counts:
            raise ValueError(f"column {label!r} has no values, every one is missing")
        values = sorted(counts)
        n = sum(counts.values())
        windows.append({
            "label": label,
            "start_ms": 0,
            "end_ms": 0,
            "n": float(n),
            "columns": n,
            "missing": len(columns[label]) - n,
            "lo": values,
            "hi": values,
            "c": [counts[v] for v in values],
        })  # fmt: skip
    return {
        "kind": "histogram",
        "mark": layer["mark"],
        "panel": panel.to_dict(),
        "dataset": meta.to_dict(),
        "effective_step_ms": meta.step_ms,  # 0: no time axis
        "value_merge": 1,
        "series": [{"id": "sample", "labels": {}, "windows": windows}],
        "caveats": [],
    }
