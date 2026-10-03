"""Over the line budget: which series a line chart draws, and how the rest are summarised (14y).

A line chart draws at most `LINE_SERIES_BUDGET` items. With more series, show() does not refuse:

* series of one metric that differ only by a member label (pod, instance, host...) are one
  fleet: drawn as the fleet view (spread band, median, outlying members), said so;
* otherwise the `keep` series that stand out most are drawn as lines, ranked by their largest
  distance from the median across all series at the same step (ties: the larger peak), and
  every other series is folded into one "others" band: their median (line) and the min of
  their minimums to the max of their maximums (envelope), with how many reported per step.

Nothing is dropped silently: the panel names what was summarised (a located caveat), and the
caller's warning lists it. Percentiles and declared intervals are never pooled into a band.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence

import polars as pl
import pyarrow as pa

from telemetry_nerd.model.caveats import series_name

OTHERS_ID = "others"
MAX_NAMED = 10  # series named in a note; the rest counted
#: labels whose values name members of one fleet (one metric, many like members)
MEMBER_LABELS = frozenset({
    "pod", "instance", "host", "hostname", "container", "container_id", "node", "pod_name",
    "k8s_pod_name", "k8s_node_name", "k8s_container_name", "replica", "shard", "cpu", "device",
})  # fmt: skip
RANK_TEXT = "largest distance from the median across all series at the same step"


def member_label(labels: Sequence[Mapping[str, str]]) -> str | None:
    """The member label when the series are members of one group: one metric (or no name),
    and every label that varies across them names a member. None otherwise."""
    if len(labels) < 2:
        return None
    if len({lb.get("__name__") for lb in labels}) > 1:
        return None
    keys = {k for lb in labels for k in lb if k != "__name__"}
    varying = sorted(k for k in keys if len({lb.get(k) for lb in labels}) > 1)
    if not varying or not set(varying) <= MEMBER_LABELS:
        return None
    return next((k for k in sorted(MEMBER_LABELS) if k in varying and _identifies(labels, k)),
                None)  # fmt: skip


def _identifies(labels: Sequence[Mapping[str, str]], key: str) -> bool:
    vals = [lb.get(key) for lb in labels]
    return None not in vals and len(set(vals)) == len(vals)


def rank(buckets: pa.Table, series_ids: Sequence[str]) -> list[str]:
    """Series ids, the most outstanding first (`RANK_TEXT`; ties: larger |peak|, then id).
    Series without a finite value come last."""
    df = pl.from_arrow(buckets)
    assert isinstance(df, pl.DataFrame)
    df = (
        df.select("ts_ms", "series_id", "avg")
        .with_columns(pl.col("avg").fill_nan(None))
        .drop_nulls("avg")
    )
    med = df.group_by("ts_ms").agg(pl.col("avg").median().alias("m"))
    scored = (
        df.join(med, on="ts_ms")
        .with_columns(dev=(pl.col("avg") - pl.col("m")).abs(), peak=pl.col("avg").abs())
        .group_by("series_id")
        .agg(pl.col("dev").max(), pl.col("peak").max())
    )
    score = {r["series_id"]: (r["dev"], r["peak"]) for r in scored.to_dicts()}
    have = sorted(score, key=lambda s: (-score[s][0], -score[s][1], s))
    return have + sorted(s for s in series_ids if s not in score)


def names(sids: Sequence[str], labels: Mapping[str, Mapping[str, str]]) -> list[str]:
    """Short names: only the labels that differ among all the series."""
    keys = {k for lb in labels.values() for k in lb}
    varying = {k for k in keys if len({lb.get(k) for lb in labels.values()}) > 1}
    out = []
    for s in sids:
        lb = labels.get(s) or {}
        out.append(series_name({k: v for k, v in lb.items() if k in varying} or lb) if lb else s)
    return out


def listed(items: Sequence[str]) -> str:
    shown = ", ".join(items[:MAX_NAMED])
    return shown + (f" and {len(items) - MAX_NAMED} more" if len(items) > MAX_NAMED else "")


def cut_note(keep_names: Sequence[str], other_names: Sequence[str], budget: int) -> str:
    """What the panel shows and what it summarises, in one sentence (panel note, warning)."""
    total = len(keep_names) + len(other_names)
    return (f"{total} series exceed the line budget ({budget}): drawn as lines are the "
            f"{len(keep_names)} with the {RANK_TEXT} ({listed(keep_names)}); the other "
            f"{len(other_names)} ({listed(other_names)}) are one grey 'others' band: their "
            "median (line) and min-max (envelope). None is dropped: query or filter them to see "
            "them one by one, or aggregate by a coarser label")  # fmt: skip


def split(series: list[dict], keep: Sequence[str], other_names: Sequence[str]) -> list[dict]:
    """`series_payload` rows -> the kept rows (in `keep` order), then one "others" row: per
    step, the median of the others' values, the min of their minimums, the max of their
    maximums, their summed counts, and how many reported (`reporting`)."""
    by_id = {s["id"]: s for s in series}
    kept = [by_id[s] for s in keep if s in by_id]
    rest = [s for s in series if s["id"] not in set(keep)]
    if not rest:
        return kept
    cols: dict[int, dict[str, list]] = {}
    for s in rest:
        for i, t in enumerate(s["ts"]):
            c = cols.setdefault(t, {"avg": [], "min": [], "max": [], "count": []})
            for f in ("avg", "min", "max", "count"):
                v = s[f][i]
                if v is not None and not math.isnan(v):
                    c[f].append(v)
    ts = sorted(cols)
    summary = {
        "id": OTHERS_ID,
        "labels": {},
        "ts": ts,
        "avg": [statistics.median(cols[t]["avg"]) if cols[t]["avg"] else None for t in ts],
        "min": [min(cols[t]["min"]) if cols[t]["min"] else None for t in ts],
        "max": [max(cols[t]["max"]) if cols[t]["max"] else None for t in ts],
        "count": [sum(cols[t]["count"]) if cols[t]["count"] else None for t in ts],
        "reporting": [len(cols[t]["avg"]) for t in ts],
        "summary": {
            "kind": "others",
            "members": len(rest),
            "names": list(other_names[:MAX_NAMED]),
            "more": max(0, len(other_names) - MAX_NAMED),
            "line": "median",
            "band": "min-max",
        },
    }
    return [*kept, summary]
