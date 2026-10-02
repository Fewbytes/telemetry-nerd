"""Fleet panel payload: the member x time z matrix behind the heatmap view (bead lkn.11).

Kept apart from fleet_ops (statistics, summary, band payload): this only shapes analyse()'s
`z` (deviation from the other members in fleet-sigma units) for drawing.
"""

from __future__ import annotations

import math

import numpy as np

from telemetry_nerd.analysis.fleet import Fleet
from telemetry_nerd.core.fleet_ops import FleetOps

MAX_HEAT_ROWS = 200  # more members than this: the most deviating rows only, disclosed
Z_CLIP = 12.0  # +-inf (zero-spread steps) and wild values stay finite on the wire
Z_CAP = 6.0  # the colour scale saturates here


def _row(z: np.ndarray) -> list[float | None]:
    out: list[float | None] = []
    for v in z:
        v = float(v)
        out.append(None if math.isnan(v) else round(max(-Z_CLIP, min(Z_CLIP, v)), 1))
    return out


def heat_rows(f: Fleet, names: list[str]) -> dict:
    """Rows sorted for reading deviation: higher outliers on top (strongest first), then the rest
    by median z (high to low), then lower outliers at the bottom (strongest last). Gaps are None;
    first/last say whether a gap is before the member's first or after its last report."""
    m_ = f.z.shape[0]
    rank = {o.member: r for r, o in enumerate(f.outliers)}
    direction = {o.member: o.direction for o in f.outliers}
    with np.errstate(all="ignore"):
        med = np.where(np.isnan(f.z).all(axis=1), 0.0, np.nan_to_num(np.nanmedian(f.z, axis=1)))
        mag = np.where(
            np.isnan(f.z).all(axis=1), 0.0, np.nan_to_num(np.nanpercentile(np.abs(f.z), 90, axis=1))
        )
    top = sorted((i for i in rank if direction[i] == "higher"), key=lambda i: rank[i])
    bottom = sorted((i for i in rank if direction[i] == "lower"), key=lambda i: -rank[i])
    rest = [i for i in range(m_) if i not in rank]
    room = max(0, MAX_HEAT_ROWS - len(top) - len(bottom))
    if len(rest) > room:
        rest = sorted(rest, key=lambda i: -mag[i])[:room]
    rest.sort(key=lambda i: -med[i])
    order = top + rest + bottom
    return {
        "z_cap": Z_CAP,
        "rows_total": m_,
        "rows": [
            {
                "id": names[i], "z": _row(f.z[i]), "rank": rank.get(i),
                "first": int(f.first_seen[i]), "last": int(f.last_seen[i]),
            }
            for i in order
        ],
    }  # fmt: skip


def heat(ops: FleetOps, dataset_id: str, cfg: dict) -> dict:
    """Heatmap payload for a stored dataset (the analysis is memoised by FleetOps.run)."""
    run = ops.run(dataset_id, cfg.get("by"), cfg.get("scale", "auto"), cfg.get("normalise", "none"))
    return heat_rows(run.fleet, run.names)
