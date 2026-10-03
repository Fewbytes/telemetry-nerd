"""Seasonal comparison over datasets (bead lkn.2): fetch previous cycles, compare, summarise.

The statistics live in analysis/seasonal.py. Each previous cycle is its own dataset (same expr,
step and source as the window, fetched through the cache); the configuration that names them is
stored on a `seasonal` panel layer, so a panel recomputes without refetching.
Design: docs/superpowers/specs/2026-10-02-seasonal-compare-design.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from telemetry_nerd.analysis import sources
from telemetry_nerd.analysis.seasonal import (
    DEFAULT_K,
    EXCLUDED_SOURCE,
    SCHEMES,
    Comparison,
    Cycle,
    choose,
    compare,
    crosses_dst,
    point_shifts,
    scale_of,
)
from telemetry_nerd.core.signal_ops import SERIES_BUDGET, SignalOps, human_period
from telemetry_nerd.core.wire import (
    Memo,
    add_caveats,
    measurement_caveats,
    sig,
    sig_list,
    sig_pair,
    statistic,
)
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.time import format_duration, iso

MAX_POINTS = 2000
#: comparison verdict -> source (spec §5.4): unusual is beyond the cycle-to-cycle envelope
VERDICT_SOURCE = {"unusual": "special_cause", "usual": "common_cause"}
DAY_S, WEEK_S = 86_400, 7 * 86_400
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

QUANTILE_REFUSAL = (
    "compare_seasonal refused on a percentile series: a reference band across cycles would "
    "aggregate percentiles over time (hint: compare the histogram per cycle: query_distribution "
    "the histogram, then show(mark='histogram', windows=[now, the same window last week]); or "
    "compare the request rate or a threshold count from fraction_over with compare_seasonal)"
)


def _moved(c: Cycle, nominal: int) -> bool:
    """Some point of the cycle is not exactly j nominal periods back (DST: 23h/25h days)."""
    if c.shifts_ms is None:
        return c.shift_ms != nominal
    s = c.shifts_ms[~np.isnan(c.shifts_ms)]
    return bool(np.any(s != nominal))


def requested_schemes(cycles: list[str] | None) -> list[str]:
    """The requested cycle schemes in SCHEMES order (default: all); unknown ones are refused."""
    cycles = list(cycles or SCHEMES)
    bad = [c for c in cycles if c not in SCHEMES]
    if bad:
        raise ValueError(f"unknown cycles {bad}: use {', '.join(SCHEMES)}")
    return [s for s in SCHEMES if s in cycles]


def scheme_shifts(
    meta: DatasetMeta, n: int, schemes: list[str], tz: str
) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    """Per scheme that applies, the (k, n) per-point shifts of its DEFAULT_K previous cycles;
    per scheme that does not, why. An unknown timezone, or no scheme applying, is refused."""
    shifts: dict[str, np.ndarray] = {}
    unavailable: dict[str, str] = {}
    for scheme in schemes:
        try:
            shifts[scheme] = point_shifts(
                meta.start_ms, n, meta.step_ms, scheme, DEFAULT_K[scheme], tz
            )
        except ValueError as e:
            if "timezone" in str(e):
                raise
            unavailable[scheme] = str(e)
    if not shifts:
        raise ValueError("no cycle applies: " + "; ".join(unavailable.values()))
    return shifts, unavailable


def seasonal_hint(dataset_id: str, periods: list[tuple[float, float, float]]) -> str | None:
    """A suggestion when a significant period (period_s, lo_s, hi_s) covers 1d or 1w."""
    for p, lo, hi in periods:
        for name, target in (("daily", DAY_S), ("weekly", WEEK_S)):
            if lo <= target <= hi or abs(p - target) <= 0.05 * target:
                return (
                    f"{name} cycle ({human_period(p)}): ask whether now is unusual for this time "
                    f'of {"day" if name == "daily" else "week"} with compare_seasonal("{dataset_id}")'
                )
    return None


class SeasonalOps:
    def __init__(
        self,
        datasets: DatasetStore,
        signal: SignalOps,
        query: Callable[..., Awaitable[dict]],
        profile_period: Callable[[str, str], list[str]] = lambda s, e: [],
    ) -> None:
        self._datasets = datasets
        self._signal = signal
        self._query = query
        self._profile_period = profile_period
        self._last: dict[str, dict] = {}  # dataset -> config of the latest compare_seasonal
        self._memo: Memo[tuple] = Memo()

    # preconditions --------------------------------------------------------------------
    def check(self, dataset_id: str):
        meta = self._datasets.meta(dataset_id)
        if meta.representation == "quantile":
            raise ValueError(QUANTILE_REFUSAL)
        self._signal.check(dataset_id, "compare_seasonal")
        if meta.derived:
            raise ValueError(
                f"{dataset_id} is filtered; compare the raw series "
                f"(hint: compare_seasonal({meta.derived['from']}))"
            )
        n = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        if n > MAX_POINTS:
            raise ValueError(
                f"{n} points per series (max {MAX_POINTS}): every previous cycle is fetched at this "
                "step (hint: re-query the window at a coarser step)"
            )
        if self._datasets.series_count(dataset_id) > SERIES_BUDGET:
            raise ValueError(
                f"more than {SERIES_BUDGET} series (hint: aggregate first, e.g. sum by (...))"
            )
        return meta

    # fetch ----------------------------------------------------------------------------
    async def fetch(
        self,
        dataset_id: str,
        cycles: list[str] | None,
        tz: str,
        exclude: list[str] | None,
        actor: str,
    ) -> dict:
        meta = self.check(dataset_id)
        wanted = requested_schemes(cycles)
        days = []
        for d in exclude or []:
            if not _DATE.match(d.strip()):
                raise ValueError(f"exclude takes local dates like 2026-12-25, got {d!r}")
            days.append(d.strip())
        n = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        grid = meta.start_ms + np.arange(n, dtype=np.int64) * meta.step_ms
        refs: dict[str, list[dict]] = {}
        all_shifts, unavailable = scheme_shifts(meta, n, wanted, tz)
        for scheme, shifts in all_shifts.items():
            refs[scheme] = []
            for j, row in enumerate(shifts, 1):
                ok = ~np.isnan(row)
                if not ok.any():
                    continue
                at = grid[ok] - row[ok].astype(np.int64)
                a, b = int(at.min()), int(at.max())
                out = await self._query(
                    meta.expr,
                    start=str(a),
                    end=str(b),
                    step=format_duration(meta.step_ms),
                    source=meta.source,
                    actor=actor,
                )
                refs[scheme].append(
                    {"j": j, "shift_ms": int(row[ok][0]), "start_ms": a, "end_ms": b,
                     "dataset": out["dataset"]}
                )  # fmt: skip
        cfg = {"tz": tz, "exclude": days, "refs": refs, "unavailable": unavailable}
        self._last[dataset_id] = cfg
        return cfg

    def last_config(self, dataset_id: str) -> dict:
        cfg = self._last.get(dataset_id)
        if cfg is None:
            raise ValueError(
                f'no seasonal comparison for {dataset_id} yet (hint: compare_seasonal("{dataset_id}") '
                "first; it fetches the previous cycles)"
            )
        return cfg

    # analysis -------------------------------------------------------------------------
    def _values(self, dataset_id: str, targets: np.ndarray, step: int) -> dict:
        """Per series: the value at each target time (bucket end), NaN where missing or where
        the target is NaN (a local time that did not exist that day)."""
        _, result = self._datasets.get(dataset_id)
        df = pl.from_arrow(result.buckets)
        assert isinstance(df, pl.DataFrame)
        df = df.with_columns(pl.col("avg").fill_nan(None)).drop_nulls("avg")
        ok = ~np.isnan(targets)
        out: dict[str, np.ndarray] = {}
        if not ok.any():
            return out
        t0 = int(np.nanmin(targets))
        want = np.where(ok, (np.nan_to_num(targets) - t0) // step, -1).astype(np.int64)
        size = int(want.max()) + 1
        for (sid,), g in df.group_by("series_id", maintain_order=True):
            idx = (g["ts_ms"].to_numpy() - t0) // step
            keep = (idx >= 0) & (idx < size)
            lut = np.full(size, np.nan)
            lut[idx[keep]] = g["avg"].to_numpy()[keep]
            out[str(sid)] = np.where(ok, lut[np.clip(want, 0, size - 1)], np.nan)
        return out

    def _excluded_js(self, cfg: dict, scheme: str, meta) -> set[int]:
        if not cfg["exclude"]:
            return set()
        zone = ZoneInfo(cfg["tz"])
        days = {date.fromisoformat(d) for d in cfg["exclude"]}
        out = set()
        for ref in cfg["refs"][scheme]:
            a0 = ref.get("start_ms", meta.start_ms - ref["shift_ms"])
            b0 = ref.get("end_ms", meta.end_ms - ref["shift_ms"])
            a = datetime.fromtimestamp((a0 - meta.step_ms) / 1000, UTC)
            b = datetime.fromtimestamp(b0 / 1000, UTC)
            d0, d1 = a.astimezone(zone).date(), b.astimezone(zone).date()
            span = {d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)}
            if span & days:
                out.add(ref["j"])
        return out

    @staticmethod
    def _shifts(meta, cfg: dict) -> dict[str, np.ndarray]:
        """Per scheme, (k, n) per-point shifts: local wall-clock alignment (recomputed from tz,
        so the stored configuration stays small)."""
        n = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        out = {}
        for scheme, refs in cfg["refs"].items():
            k = max((r["j"] for r in refs), default=0)
            out[scheme] = (
                point_shifts(meta.start_ms, n, meta.step_ms, scheme, k, cfg["tz"])
                if k
                else np.zeros((0, n))
            )
        return out

    def run(self, dataset_id: str, cfg: dict) -> tuple:
        key = dataset_id + json.dumps(cfg, sort_keys=True)
        if (hit := self._memo.get(key)) is not None:
            return hit
        meta, result = self._datasets.get(dataset_id)
        step, n = meta.step_ms, (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        # buckets carry their END time: the grid is the bucket end times of the window
        grid = meta.start_ms + np.arange(n, dtype=np.int64) * step
        now = self._values(dataset_id, grid.astype(float), step)
        labels = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
        shifts = self._shifts(meta, cfg)
        per_scheme = {
            scheme: {
                ref["j"]: (ref, shifts[scheme][ref["j"] - 1],
                           self._values(ref["dataset"], grid - shifts[scheme][ref["j"] - 1], step))
                for ref in refs
            }
            for scheme, refs in cfg["refs"].items()
        }  # fmt: skip
        excl = {s: self._excluded_js(cfg, s, meta) for s in cfg["refs"]}
        out = {}
        for sid, y in now.items():
            schemes = {
                s: [
                    Cycle(s, j, ref["shift_ms"], ref.get("start_ms", meta.start_ms - ref["shift_ms"]),
                          vals.get(sid, np.full(n, np.nan)), sh)
                    for j, (ref, sh, vals) in sorted(cyc.items())
                ]
                for s, cyc in per_scheme.items()
            }  # fmt: skip
            pick, scores = choose(y, schemes, step, excl)
            scale = scale_of(y, [c for cs in schemes.values() for c in cs])
            # nothing usable: report the most specific scheme's shortfall
            scheme = pick if pick is not None else [s for s in SCHEMES if s in schemes][-1]
            cmp_ = compare(y, schemes[scheme], step, excl.get(scheme, set()), scale)
            out[sid] = (labels.get(sid, {}), y, cmp_, scores)
        res = (meta, out)
        self._memo.put(key, res)
        return res

    # outputs --------------------------------------------------------------------------
    @staticmethod
    def label(c: Comparison, tz: str, span_ms: int) -> str:
        k = len(c.kept)
        where = "UTC" if tz == "UTC" else f"local time {tz}"
        if c.scheme == "previous":
            return f"median of the {k} preceding {format_duration(span_ms)} windows (no daily/weekly cycle helps)"  # fmt: skip
        unit = "days" if c.scheme == "1d" else "weeks"
        return f"median of the same window on the previous {k} {unit}, aligned by {where}"

    def summary(self, dataset_id: str, cfg: dict) -> dict:
        meta, res = self.run(dataset_id, cfg)
        span = meta.end_ms - meta.start_ms + meta.step_ms
        caveats: list[str] = []
        if cfg["tz"] != "UTC" and any(crosses_dst(x) for x in self._shifts(meta, cfg).values()):
            caveats.append("dst_wall_clock")
        series = []
        for labels, _, c, scores in res.values():
            series.append(self._series(dataset_id, meta, cfg, labels, c, scores, span))
            add_caveats(caveats, c.caveats)
        profile = self._profile_period(meta.source, meta.expr)
        alignment = (
            "UTC: cycles are whole days/weeks in UTC (human-driven load follows local time; "
            "pass tz=... to align by local time across DST)"
            if cfg["tz"] == "UTC"
            else f"local time {cfg['tz']}: shifts follow local calendar days (23h/25h across DST)"
        )
        return {
            "dataset": dataset_id,
            "window": {"start": iso(meta.start_ms), "end": iso(meta.end_ms),
                       "step": format_duration(meta.step_ms)},
            "alignment": alignment,
            "schemes": {
                **{s: f"{len(r)} cycles fetched" for s, r in cfg["refs"].items()},
                **{s: f"unavailable: {why}" for s, why in cfg["unavailable"].items()},
            },
            "profile_period": profile or None,
            "series": series,
            "caveats": caveats,
            # spec §5.4: dataset-wide measurement-system items; per series in series[].variation
            "variation": sources.measurement_items(measurement_caveats(meta)),
            "draw": f'show("{dataset_id}", question, mark="seasonal")',
        }  # fmt: skip

    def _series(self, dataset_id, meta, cfg, labels, c: Comparison, scores, span) -> dict:
        def cyc(cy: Cycle) -> dict:
            return {"j": cy.j, "start": iso(cy.start_ms),
                    "shift_h": sig(cy.shift_ms / 3_600_000)}  # fmt: skip

        out: dict = {
            "labels": labels,
            "verdict": c.verdict,
            "direction": c.direction,
            "reasons": c.reasons,
            **({"source": src} if (src := VERDICT_SOURCE.get(c.verdict)) else {}),
            # spec §5.4: each finding labelled (the band is the common-cause envelope)
            "variation": c.variation + sources.measurement_items(c.caveats),
            "reference": {
                "scheme": c.scheme,
                "label": self.label(c, cfg["tz"], span),
                "cycles": [cyc(x) for x in c.kept],
                "excluded": [
                    {**cyc(x), "reason": why, "source": EXCLUDED_SOURCE[why]}
                    for x, why in c.excluded
                ],
                "scores": {k: sig(v, 3) for k, v in scores.items()},
            },
        }
        period_ms = {"1d": DAY_S * 1000, "1w": WEEK_S * 1000}.get(c.scheme)
        if period_ms and (moved := [x.j for x in c.kept if _moved(x, x.j * period_ms)]):
            out["reference"]["dst_shifted"] = moved  # cycles 23h/25h-per-day back, not 24h
        if c.level is None:
            return out
        lv, unit = c.level, ("ratio" if c.scale == "log" else "difference")
        method = (
            "now / median of previous cycles over the window; interval = 90% range of a normal "
            f"cycle (t with {len(c.kept) - 1} df from the spread across the {len(c.kept)} cycles)"
        )
        out |= {
            "scale": c.scale,
            "n": c.n,
            "n_eff": sig(c.n_eff, 3),
            unit: {
                "value": sig(lv.ratio), "normal_90": sig_pair(lv.normal),
                "interval_99": sig_pair(lv.p_interval),
                "previous_cycles": [sig(v, 3) for v in lv.cycle_means], "flagged": lv.flagged,
                "evidence": statistic(
                    dataset_id, f"seasonal_{unit}", sig(lv.ratio), sig_pair(lv.normal), method,
                    {"reference": c.scheme, "cycles": len(c.kept), "tz": cfg["tz"],
                     "step": format_duration(meta.step_ms), "n": c.n},
                    source=sources.SPECIAL if lv.flagged else sources.COMMON,
                ),
            },
        }  # fmt: skip
        if c.outside is not None:
            out["outside_band"] = {
                "share": sig(c.outside.share, 3), "nominal": 0.1,
                "previous_cycles": [sig(v, 3) for v in c.outside.previous],
                "p": sig(c.outside.p, 2), "flagged": c.outside.flagged,
                "source": sources.SPECIAL if c.outside.flagged else sources.COMMON,
            }  # fmt: skip
        if c.extremes is not None:
            e = c.extremes
            top = max(e.points, key=lambda q: abs(q[1])) if e.points else None
            out["extremes"] = {
                "threshold_z": sig(e.threshold, 3), "points": len(e.points),
                "max_z": sig(top[1], 3) if top else None,
                "at": iso(meta.start_ms + top[0] * meta.step_ms) if top else None,
                "heavy_tails": e.heavy_tails, "flagged": e.flagged,
                "source": sources.SPECIAL if e.flagged else sources.COMMON,
            }  # fmt: skip
        return out

    def panel(self, dataset_id: str, cfg: dict) -> dict:
        meta, res = self.run(dataset_id, cfg)
        span = meta.end_ms - meta.start_ms + meta.step_ms
        n = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        ts = [int(meta.start_ms + i * meta.step_ms) for i in range(n)]
        series = []
        caveats: list[str] = []
        for sid, (labels, y, c, _) in res.items():
            add_caveats(caveats, c.caveats)
            item: dict = {
                "id": sid, "labels": labels, "ts": ts, "now": sig_list(y), "verdict": c.verdict,
                "direction": c.direction, "reasons": c.reasons, "scheme": c.scheme,
                "label": self.label(c, cfg["tz"], span), "scale": c.scale,
                "cycles": [{"j": x.j, "start_ms": x.start_ms, "values": sig_list(x.values)} for x in c.kept],
                "excluded": [{"j": x.j, "start_ms": x.start_ms, "reason": why,
                              "source": EXCLUDED_SOURCE[why]} for x, why in c.excluded],
                "n": c.n,
            }  # fmt: skip
            if c.centre is not None:
                ratio = c.scale == "log"
                conv = np.exp if ratio else (lambda a: a)
                item |= {
                    "centre": sig_list(c.centre), "lo": sig_list(c.lo), "hi": sig_list(c.hi),
                    "ratio": {"kind": "ratio" if ratio else "difference", "value": sig_list(conv(c.d)),
                              "lo": sig_list(conv(c.d_lo)), "hi": sig_list(conv(c.d_hi))},
                    "flagged": [{"ts": ts[i], "value": sig(float(y[i])), "z": sig(z, 3)}
                                for i, z in (c.extremes.points if c.extremes else [])],
                    "n_eff": sig(c.n_eff, 3),
                }  # fmt: skip
            series.append(item)
        return {
            "kind": "seasonal",
            "effective_step_ms": meta.step_ms,
            "tz": cfg["tz"],
            "series": series,
            "caveats": caveats,
        }
