"""Panel data payloads: what the UI needs to draw a panel, built from stored datasets.

Pure functions over datasets and panel specs (no I/O beyond reading datasets)."""

from __future__ import annotations

import json
import math

import polars as pl

from telemetry_nerd.analysis.distlod import (
    FACET_HEIGHT_MULTI,
    FACET_HEIGHT_SINGLE,
    HIST_PX_PER_BAR,
    PX_PER_COLUMN,
    PX_PER_ROW,
    merge_values,
    rebucket_time,
    window_histogram,
)
from telemetry_nerd.analysis.filters import removed_table
from telemetry_nerd.analysis.marginal import (
    SAMPLE_N_MIN,
    histogram_of,
    pooled_window,
    sample_bins,
    step_values,
)
from telemetry_nerd.analysis.profile import band_at
from telemetry_nerd.analysis.quantiles import column_quantiles
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.charts.indexed import shifted, window_baselines
from telemetry_nerd.charts.spec import ChartSpec
from telemetry_nerd.model.bucket_state import State, coarsen, compute, grid
from telemetry_nerd.model.distribution import DIST_N_MIN, QUANTILE_CHOICES
from telemetry_nerd.model.time import TimeRange, format_duration, iso
from telemetry_nerd.workspace.store import Panel


def state_payload(states) -> list[dict]:
    """bucket_state for series with any non-OK bucket (an all-ok panel draws no rug)."""
    df = pl.from_arrow(states)
    if df.is_empty():
        return []
    bad = df.filter(pl.col("state") != int(State.OK))["series_id"].unique()
    out = []
    for (sid,), g in (
        df.filter(pl.col("series_id").is_in(bad.implode()))
        .sort("ts_ms")
        .group_by("series_id", maintain_order=True)
    ):
        out.append({
            "id": sid, "ts": g["ts_ms"].to_list(), "state": g["state"].to_list(),
            "observed": g["observed"].to_list(), "expected": g["expected"].to_list(),
            "flags": g["flags"].to_list(),
        })  # fmt: skip
    return sorted(out, key=lambda s: s["id"])


def column_states(meta, dist):
    """Distribution columns as presence buckets: a returned column is observed even when n = 0."""
    cols = pl.from_arrow(dist.columns).select(
        "ts_ms", "series_id", pl.col("n").cast(pl.Float64).alias("avg"),
        pl.col("n").cast(pl.Float64).alias("min"), pl.col("n").cast(pl.Float64).alias("max"),
        pl.lit(1, pl.Int64).alias("count"),
    )  # fmt: skip
    return compute(
        cols.to_arrow(), dist.series["series_id"].to_pylist(), start_ms=meta.start_ms,
        end_ms=meta.end_ms, step_ms=meta.step_ms, resolution_ms=meta.step_ms, mode="presence",
        failed=[tuple(f) for f in meta.failed_spans],
    )  # fmt: skip


def series_labels(series_table) -> dict[str, dict]:
    """series_id -> labels; labels are self-produced, so a bad row never blocks a panel."""
    out: dict[str, dict] = {}
    for r in series_table.to_pylist():
        try:
            out[r["series_id"]] = json.loads(r["labels"])
        except (TypeError, json.JSONDecodeError):
            out[r["series_id"]] = {}
    return out


def series_payload(table, labels: dict[str, dict]) -> list[dict]:
    out = []
    for (sid,), group in pl.DataFrame(table).group_by("series_id", maintain_order=True):
        cols = {c: group[c].to_list() for c in ("ts_ms", "avg", "min", "max", "count")}
        cols["ts"] = cols.pop("ts_ms")
        out.append({"id": sid, "labels": labels.get(sid, {}), **cols})
    return out


def empty_window(w: dict) -> dict:
    return {"start_ms": w["start_ms"], "end_ms": w["end_ms"], "n": 0.0, "columns": 0,
            "lo": [], "hi": [], "c": []}  # fmt: skip


def histogram_panel_data(panel, meta, dist, labels, caveats, width_px) -> dict:
    layer = panel.spec["layers"][0]
    raw = pl.from_arrow(dist.rows)
    rows, value_merge = merge_values(raw, dist.scheme, max(4, width_px // HIST_PX_PER_BAR))
    cols = pl.from_arrow(dist.columns)
    states = pl.from_arrow(column_states(meta, dist))

    def hists(frame):
        return [
            window_histogram(frame, cols, meta.step_ms, w["start_ms"], w["end_ms"])
            for w in layer["windows"]
        ]

    merged = hists(rows)
    # cumulative views (ecdf, quantile, ccdf) must read source buckets, never merged bars
    exact = hists(raw) if value_merge > 1 else None

    def window(k, w, sid):
        out = {"label": w["label"], **(merged[k].get(sid) or empty_window(w))}
        if exact is not None:
            src = exact[k].get(sid) or empty_window(w)
            out["source"] = {"lo": src["lo"], "hi": src["hi"], "c": src["c"]}
        # columns are (ts - step, ts], so the window's columns have ts in (start, end]
        g = states.filter(
            (pl.col("series_id") == sid)
            & (pl.col("ts_ms") > out["start_ms"])
            & (pl.col("ts_ms") <= out["end_ms"])
        )
        out["expected_columns"] = len(grid(out["start_ms"] + 1, out["end_ms"], meta.step_ms))
        out["unknown"] = bool((g["state"] == int(State.UNKNOWN)).any())
        return out

    series = [
        {
            "id": sid,
            "labels": lb,
            "windows": [window(k, w, sid) for k, w in enumerate(layer["windows"])],
        }
        for sid, lb in labels.items()
    ]
    n_min = meta.n_min or 0
    if any(0 < w["n"] < n_min for s in series for w in s["windows"]) and "low_count" not in caveats:
        caveats.append("low_count")
    return {"kind": "histogram", "mark": layer["mark"], "panel": panel.to_dict(),
            "dataset": meta.to_dict(), "effective_step_ms": meta.step_ms,
            "value_merge": value_merge, "series": series, "caveats": caveats}  # fmt: skip


def heatmap_panel_data(panel, meta, dist, labels, caveats, width_px) -> dict:
    rows = pl.from_arrow(dist.rows)
    cols = pl.from_arrow(dist.columns).with_columns(pl.lit(1, pl.Int64).alias("cover"))
    n_cols = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    factor = max(1, math.ceil(n_cols / max(1, width_px // PX_PER_COLUMN)))
    step = meta.step_ms * factor
    if factor > 1:
        rows, cols = rebucket_time(rows, cols, step)
    # source buckets holding each q: time-summed counts (additive), never value-merged
    bands = column_quantiles(rows, cols, QUANTILE_CHOICES)
    states = column_states(meta, dist)
    if factor > 1:
        states = coarsen(states, step)
    by_sid = {s["id"]: s for s in state_payload(states)}
    facet_h = FACET_HEIGHT_SINGLE if len(labels) <= 1 else FACET_HEIGHT_MULTI
    rows, value_merge = merge_values(rows, dist.scheme, max(4, facet_h // PX_PER_ROW))
    series = []
    for sid, lb in labels.items():
        c = cols.filter(pl.col("series_id") == sid)
        r = rows.filter(pl.col("series_id") == sid)
        series.append({
            "id": sid, "labels": lb,
            "ts": c["ts_ms"].to_list(), "n": c["n"].to_list(), "cover": c["cover"].to_list(),
            "cells": {"ts": r["ts_ms"].to_list(), "lo": r["bucket_lo"].to_list(),
                      "hi": r["bucket_hi"].to_list(), "c": r["count"].to_list()},
            "quantiles": bands.get(sid, {}),
            "state": by_sid.get(sid),
        })  # fmt: skip
    return {
        "kind": "heatmap", "mark": panel.spec["layers"][0]["mark"],
        "panel": panel.to_dict(), "dataset": meta.to_dict(),
        "effective_step_ms": step, "value_merge": value_merge, "facet_height_px": facet_h,
        "series": series, "caveats": caveats,
    }  # fmt: skip


def marginal_payload(datasets, spec: ChartSpec, meta) -> dict | None:
    m = spec.marginal
    ref = spec.references.get(m.reference) if m else None
    if m is None or ref is None:
        return None
    head = {
        "reference": {
            "mode": ref.mode,
            "label": ref.label,
            "start_ms": ref.start_ms,
            "end_ms": ref.end_ms,
        },
        "author": m.author,
        "reason": m.reason,
    }
    if ref.dist and ref.dist_current:
        wins = []
        for did, label in ((ref.dist_current, "now"), (ref.dist, ref.label)):
            dm, dist = datasets.get_distribution(did)
            w = pooled_window(pl.from_arrow(dist.rows), pl.from_arrow(dist.columns), dm.step_ms,
                              dm.start_ms - dm.step_ms, dm.end_ms)  # fmt: skip
            wins.append(
                {
                    "label": label,
                    "start_ms": dm.start_ms,
                    "end_ms": dm.end_ms,
                    "n": 0.0,
                    "columns": 0,
                    "lo": [],
                    "hi": [],
                    "c": [],
                    **(w or {}),
                }
            )
        k = max(w.get("series", 1) for w in wins)
        what = f"observations (requests) of {meta.histogram['selector']}" + (
            f", {k} series summed" if k > 1 else ""
        )
        return {
            "basis": "distribution",
            "what": what,
            "n_min": DIST_N_MIN,
            "windows": wins,
            "excluded": [0, 0],
            **head,
        }
    _, cur = datasets.get(meta.id)
    rmeta, rres = datasets.get(ref.series)
    cv, cx, k = step_values(cur.buckets, meta.representation, meta.n_min)
    rv, rx, _ = step_values(rres.buckets, rmeta.representation, rmeta.n_min)
    edges = sample_bins(cv, rv)
    lo, hi = [a for a, _ in edges], [b for _, b in edges]
    step = format_duration(meta.step_ms)
    kind = (
        f"p{meta.quantile * 100:g} values per {step} step (n ≥ {meta.n_min} only): "
        "a distribution of percentile values, not of requests"
        if meta.representation == "quantile"
        else f"per-step values ({step} means of scrape samples): scrape samples, not requests"
    )
    what = kind + (f"; {k} series pooled" if k > 1 else "")
    wins = [{"label": label, "start_ms": s, "end_ms": e, "n": float(len(v)), "columns": len(v),
             "lo": lo, "hi": hi, "c": histogram_of(v, edges)}
            for label, s, e, v in (("now", meta.start_ms, meta.end_ms, cv),
                                   (ref.label, ref.start_ms, ref.end_ms, rv))]  # fmt: skip
    return {
        "basis": "samples",
        "what": what,
        "n_min": SAMPLE_N_MIN,
        "windows": wins,
        "excluded": [cx, rx],
        **head,
    }


def reference_series(datasets, ref, meta, width_px: int) -> list[dict]:
    """A reference dataset moved onto the panel's time grid, at the panel's resolution."""
    _, rres = datasets.get(ref.series)
    table = shifted(rres.buckets, ref.shift_ms)
    if meta.representation != "quantile":  # same range and step as the panel => identical LOD grid
        table, _ = lod(table, meta.step_ms, TimeRange(meta.start_ms, meta.end_ms), width_px)
    return [
        {
            "id": sid,
            "ts": g["ts_ms"].to_list(),
            "avg": g["avg"].to_list(),
            "count": g["count"].to_list(),
        }
        for (sid,), g in pl.DataFrame(table).group_by("series_id", maintain_order=True)
    ]


def index_payload(datasets, spec: ChartSpec, meta, result, width_px: int) -> dict | None:
    v = spec.y.selected
    if v is None or v.mode != "indexed":
        return None
    if v.baseline == "window":
        a, b = iso(meta.start_ms - meta.step_ms)[11:16], iso(meta.end_ms)[11:16]
        return {
            "baseline": "window",
            "label": f"1 = each series' mean over {a}–{b}Z",
            "values": window_baselines(result.buckets),
        }
    ref = spec.references.get(v.baseline)
    if ref is None:
        return {
            "baseline": v.baseline,
            "label": "",
            "refused": f"no {v.baseline} reference fetched",
        }
    series = reference_series(datasets, ref, meta, width_px)
    when = "last week" if ref.mode == "week" else "in the previous window"
    return {
        "baseline": v.baseline,
        "label": f"1 = the same series {when} (point by point)",
        "series": series,
    }


def signal_payload(datasets, panel: Panel, meta, width_px: int, labels) -> dict:
    """raw / removed / filter info for a panel drawn from filter() (bead 4ok.9)."""
    sig = panel.spec.get("signal")
    if not sig or len(panel.dataset_ids) < 2 or not meta.derived:
        return {}
    _, raw = datasets.get(panel.dataset_ids[1])
    rng = TimeRange(meta.start_ms, meta.end_ms)
    raw_t, _ = lod(raw.buckets, meta.step_ms, rng, width_px)
    out = {
        "raw": series_payload(raw_t, labels),
        "filter": {
            **sig,
            "edges": meta.derived.get("edges", {}),
            "period_ms": meta.derived["period_ms"],
            "period_hi_ms": meta.derived.get("period_hi_ms"),
        },
    }
    if meta.derived["op"] != "lowpass":
        _, filt = datasets.get(panel.dataset_ids[0])
        removed, _ = lod(removed_table(raw.buckets, filt.buckets), meta.step_ms, rng, width_px)
        out["removed"] = series_payload(removed, labels)
    return out


def _plain(labels: dict) -> dict:
    return {k: v for k, v in labels.items() if k != "__name__"}


def normal_payload(profile, series: list[dict], step_ms: int, window: str) -> dict:
    """Seasonal normal band per drawn series from the operating profile (bead 2as.11).

    Each bucket is compared with the same hour of the profile's period (hour of week); a profile
    without a seasonal pattern gives a flat band at its pooled range. Series are matched to
    profile series by labels (ignoring __name__, which a rate drops)."""
    if profile is None:
        return {
            "available": False,
            "reason": "the operating profile is not computed yet or the expression has none",
        }
    by_labels = {tuple(sorted(_plain(p.labels).items())): p for p in profile.series}
    bands: dict[str, dict] = {}
    unmatched: list[str] = []
    seasonal_any = False
    for s in series:
        p = by_labels.get(tuple(sorted(_plain(s["labels"]).items())))
        if p is None:
            unmatched.append(s["id"])
            continue
        if p.seasonal is not None:
            seasonal_any = True
            lo, hi = [], []
            for ts in s["ts"]:
                b = band_at(p.seasonal, ts - step_ms)
                lo.append(b.lo)
                hi.append(b.hi)
        else:
            r = p.range
            flo, fhi = (r.envelope_lo, r.envelope_hi) if profile.extremes else (r.p005, r.p995)
            lo, hi = [flo] * len(s["ts"]), [fhi] * len(s["ts"])
        bands[s["id"]] = {"ts": s["ts"], "lo": lo, "hi": hi}
    if not bands:
        return {
            "available": False,
            "reason": "no drawn series matches a profiled series (the profile covers other labels)",
        }
    kind = "same hour of week" if seasonal_any else "flat: no seasonal pattern found"
    return {
        "available": True,
        "label": f"normal range ({window}, {kind})",
        "stale": profile.stale,
        "series": bands,
        "unmatched": unmatched,
    }


def limit_payload(datasets, ctx, meta, labels, width_px: int) -> dict:
    """The bounded_by metric drawn as a limit line, or why there is none."""
    if ctx is None or ctx.limit is None:
        why = next(
            (
                n.split(": ", 1)[-1]
                for n in (ctx.notes if ctx else [])
                if n.startswith("limit_unavailable")
            ),
            None,
        )
        return {
            "available": False,
            "reason": why or "the catalog has no bounded_by relation for this metric",
        }
    _, res = datasets.get(ctx.limit.dataset)
    table, _ = lod(res.buckets, meta.step_ms, TimeRange(meta.start_ms, meta.end_ms), width_px)
    return {
        "available": True,
        "label": f"limit {ctx.limit.metric}",
        "metric": ctx.limit.metric,
        "hi": ctx.limit.hi,
        "series": series_payload(table, series_labels(res.series)),
    }


def ghost_payload(datasets, spec: ChartSpec, meta, width_px: int) -> dict:
    ref = spec.references.get("week")
    if ref is None:
        return {"available": True, "loaded": False, "label": "last week"}
    return {
        "available": True,
        "loaded": True,
        "label": ref.label or "last week",
        "series": reference_series(datasets, ref, meta, width_px),
    }
