"""Seasonal comparison for latency (bead lkn.7): the histogram per cycle.

compare_seasonal on a percentile series (with the histogram behind it) or on a distribution
dataset fetches the histogram for now and for each previous cycle at the same local phase, and
compares the cycles' window histograms: share above a threshold (exact at a bucket edge) against
the spread of the previous cycles' shares, and a shape distance per cycle. Percentiles are never
pooled or averaged across cycles. Statistics: analysis/seasonal_dist.py.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from telemetry_nerd.analysis.distlod import window_histogram
from telemetry_nerd.analysis.seasonal import crosses_dst
from telemetry_nerd.analysis.seasonal_dist import (
    Hist,
    TailComparison,
    choose_tail,
    common_edges,
    compare_tail,
    default_threshold,
    snap,
)
from telemetry_nerd.core.seasonal_ops import requested_schemes, scheme_shifts
from telemetry_nerd.core.signal_ops import SERIES_BUDGET
from telemetry_nerd.core.wire import add_caveats, sig, statistic
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.time import format_duration, iso

MAX_POINTS = 2000

NO_HISTOGRAM = (
    "compare_seasonal refused on a percentile series whose histogram is not known (summary "
    "quantiles, or a form not recognised): a reference across cycles would aggregate "
    "percentiles over time (hint: compare the histogram per cycle: query_distribution the "
    "_bucket histogram and compare_seasonal that dataset, or write the percentile as "
    "histogram_quantile(q, sum by (le) (rate(x_bucket[5m]))); or compare the request rate)"
)


def _key(labels: dict) -> str:
    return json.dumps(labels, sort_keys=True)


class SeasonalDistOps:
    def __init__(
        self,
        datasets: DatasetStore,
        query_distribution: Callable[..., Awaitable[dict]],
    ) -> None:
        self._datasets = datasets
        self._qd = query_distribution

    @staticmethod
    def applies(meta) -> bool:
        return meta.representation in ("quantile", "distribution")

    def _hists(self, dataset_id: str, w0: int, w1: int, j: int) -> dict[str, Hist]:
        """Per series (by labels): the window histogram over (w0, w1]."""
        meta, dist = self._datasets.get_distribution(dataset_id)
        rows = pl.from_arrow(dist.rows)
        cols = pl.from_arrow(dist.columns)
        assert isinstance(rows, pl.DataFrame) and isinstance(cols, pl.DataFrame)
        labels = {r["series_id"]: json.loads(r["labels"]) for r in dist.series.to_pylist()}
        expected = max(1, -(-(w1 - w0) // meta.step_ms))
        out = {}
        for sid, h in window_histogram(rows, cols, meta.step_ms, w0, w1).items():
            out[_key(labels.get(sid, {}))] = Hist(
                j, h["start_ms"], h["end_ms"], np.asarray(h["lo"], float),
                np.asarray(h["hi"], float), np.asarray(h["c"], float),
                min(1.0, h["columns"] / expected), dataset_id,
            )  # fmt: skip
        return out

    async def compare(
        self,
        dataset_id: str,
        cycles: list[str] | None,
        tz: str,
        exclude: list[str] | None,
        threshold: float | None,
        actor: str,
    ) -> dict:
        meta = self._datasets.meta(dataset_id)
        if not meta.histogram:
            raise ValueError(NO_HISTOGRAM)
        n = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        if n > MAX_POINTS:
            raise ValueError(
                f"{n} steps (max {MAX_POINTS}) (hint: re-query the window at a coarser step)"
            )
        wanted = requested_schemes(cycles)
        try:
            days = sorted({date.fromisoformat(d.strip()) for d in exclude or []})
        except ValueError as e:
            raise ValueError(f"exclude takes local dates like 2026-12-25, got {exclude!r}") from e
        sel, by = meta.histogram["selector"], list(meta.histogram.get("by") or [])
        step = meta.step_ms if meta.representation == "distribution" else max(
            meta.step_ms, 2 * meta.resolution_ms
        )  # fmt: skip
        w0, w1 = meta.start_ms - meta.step_ms, meta.end_ms  # buckets: (ts - step, ts]

        async def fetch(a: int, b: int) -> str:
            out = await self._qd(
                sel, by, start=str(a + step), end=str(b), step=format_duration(step),
                source=meta.source, actor=actor,
            )  # fmt: skip
            return out["dataset"]

        now_ds = dataset_id if meta.representation == "distribution" else await fetch(w0, w1)
        now = self._hists(now_ds, w0, w1, 0)
        refs: dict[str, list[dict[str, Hist]]] = {}
        dst = False
        excl: dict[str, set[int]] = {}
        zone = ZoneInfo(tz) if tz != "UTC" else UTC
        all_shifts, unavailable = scheme_shifts(meta, n, wanted, tz)
        for scheme, sh in all_shifts.items():
            dst |= tz != "UTC" and crosses_dst(sh)
            refs[scheme], excl[scheme] = [], set()
            for j, row in enumerate(sh, 1):
                ok = row[~np.isnan(row)]
                if ok.size == 0:
                    continue
                a, b = w0 - int(ok[0]), w1 - int(ok[-1])  # wall-clock window of cycle j
                refs[scheme].append(self._hists(await fetch(a, b), a, b, j))
                d0 = datetime.fromtimestamp(a / 1000, UTC).astimezone(zone).date()
                d1 = datetime.fromtimestamp((b - 1) / 1000, UTC).astimezone(zone).date()
                if any(d0 <= d <= d1 for d in days):
                    excl[scheme].add(j)

        series, caveats = [], (["dst_wall_clock"] if dst else [])
        for key, h_now in list(now.items())[:SERIES_BUDGET]:
            schemes = {
                s: [
                    cyc.get(key) or Hist(j, 0, 0, np.array([]), np.array([]), np.array([]), 0.0)
                    for j, cyc in enumerate(cs, 1)
                ]
                for s, cs in refs.items()
            }
            every = [h_now, *[h for hs in schemes.values() for h in hs]]
            edges = common_edges(every)
            if threshold is None:
                x, enough = default_threshold([h for hs in schemes.values() for h in hs], edges)
                how = (
                    "default: the bucket edge where the previous cycles' pooled share above is "
                    "nearest 1% (chosen from the reference, not from now)"
                )
            else:
                x, enough = snap(threshold, edges), True
                how = "requested" if x == threshold else f"requested {threshold:g}, snapped to the nearest bucket edge shared by every cycle (exact there; never interpolated)"  # fmt: skip
            pick, scores = choose_tail(h_now, schemes, x, excl)
            scheme = pick or list(schemes)[-1]
            c = compare_tail(h_now, schemes[scheme], x, scheme, excl.get(scheme, set()))
            if not enough and x is not None and "few_over_threshold" not in c.caveats:
                c.caveats.append("few_over_threshold")
            add_caveats(caveats, c.caveats)
            series.append(self._series(dataset_id, json.loads(key), c, scores, x, how, tz))
        return {
            "dataset": dataset_id,
            "kind": "distribution",
            "histogram": {"selector": sel, "by": by, "now": now_ds},
            "window": {"start": iso(w0), "end": iso(w1), "step": format_duration(step)},
            "alignment": (
                "UTC: cycles are whole days/weeks in UTC"
                if tz == "UTC"
                else f"local time {tz}: each cycle's window is the same local wall-clock window"
            ),
            "schemes": {
                **{s: f"{len(r)} cycles fetched" for s, r in refs.items()},
                **{s: f"unavailable: {why}" for s, why in unavailable.items()},
            },
            "method": (
                "per cycle: the window histogram (bucket counts summed over the window). Share "
                "above the threshold per cycle, exact at a bucket edge; band = Student t "
                "prediction interval of the cycles' logit shares (k-1 df, never tighter than "
                "binomial noise). Percentiles are never pooled or averaged across cycles."
            ),
            "series": series,
            "caveats": caveats,
            "draw": (
                "show(<a cycle's dataset>, question, mark='histogram'); no seasonal panel for "
                "histograms yet"
            ),
        }

    @staticmethod
    def _series(dataset_id, labels, c: TailComparison, scores, x, how, tz) -> dict:
        def cyc(h: Hist) -> dict:
            d: dict = {"j": h.j, "start": iso(h.start_ms), "n": sig(h.n)}
            if x is not None and h.n > 0:
                d["share_over"] = sig(h.over(x) / h.n, 3)
            d["dataset"] = h.dataset
            return d

        k = len(c.kept)
        where = "UTC" if tz == "UTC" else f"local time {tz}"
        label = (
            f"the {k} preceding windows (no daily/weekly cycle helps)"
            if c.scheme == "previous"
            else f"the same window on the previous {k} {'days' if c.scheme == '1d' else 'weeks'}, "
            f"aligned by {where}"
        )
        out: dict = {
            "labels": labels,
            "verdict": c.verdict,
            "direction": c.direction,
            "reasons": c.reasons,
            "threshold": {"x": x, "how": how},
            "now": {"n": sig(c.now.n), **({"share_over": sig(c.now.over(x) / c.now.n, 3)}
                                          if x is not None and c.now.n > 0 else {})},
            "reference": {
                "scheme": c.scheme,
                "label": label,
                "cycles": [cyc(h) for h in c.kept],
                "excluded": [{**cyc(h), "reason": why} for h, why in c.excluded],
                "scores": {s: sig(v, 3) for s, v in scores.items()},
            },
        }  # fmt: skip
        if c.share is None:
            return out
        sh = c.share
        out["share_over"] = {
            "value": sig(sh.value, 3), "normal_90": [sig(sh.normal[0], 3), sig(sh.normal[1], 3)],
            "interval_99": [sig(sh.p_interval[0], 3), sig(sh.p_interval[1], 3)],
            "centre": sig(sh.centre, 3), "flagged": sh.flagged,
            "evidence": statistic(
                dataset_id, "seasonal_share_over", sig(sh.value, 3),
                [sig(sh.normal[0], 3), sig(sh.normal[1], 3)],
                f"share above {x:g} per cycle from histogram counts; 90% range of a normal cycle "
                f"from the spread of {k} cycles (t, {k - 1} df, logit)",
                {"x": x, "reference": c.scheme, "cycles": k, "tz": tz, "n": sig(c.now.n)},
            ),
        }  # fmt: skip
        if math.isfinite(c.shape):
            prev = [v for v in c.shape_previous if math.isfinite(v)]
            out["shape"] = {
                "distance": sig(c.shape, 3),
                "previous_cycles": [sig(v, 3) for v in prev],
                "beyond_previous": bool(prev) and c.shape > max(prev),
                "method": "largest |CDF difference| at the shared bucket edges: now vs the kept "
                          "cycles' summed counts, each cycle vs the others (descriptive: with k "
                          "cycles, now exceeds all of them by chance 1/(k+1) of the time)",
            }  # fmt: skip
        return out
