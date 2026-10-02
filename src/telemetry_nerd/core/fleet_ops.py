"""fleet: many series of one metric analysed as a group (bead lkn.3).

The statistics live in analysis/fleet.py. This layer checks preconditions (no percentiles, one
unit, enough members), lays the dataset on its step grid (members x steps, NaN = no report),
names members, and shapes the summary for Claude and the `fleet` panel payload.
Design: docs/superpowers/specs/2026-10-02-fleet-analysis-design.md.
"""

from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable

import numpy as np
import polars as pl

from telemetry_nerd.analysis.fleet import (
    ALPHA,
    EXCURSIONS,
    MIN_MEMBERS,
    Fleet,
    Outlier,
    analyse,
    trimmed_interval,
)
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.catalog.rules import Facts
from telemetry_nerd.core.signal_ops import SignalOps
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.bucket_state import grid
from telemetry_nerd.model.time import TimeRange, format_duration, iso

MAX_STEPS = 1440  # longer ranges are averaged per member to a coarser step first
MAX_LISTED = 10
MAX_DRAWN = 6
CHURN_TOL = 0.05  # appeared / stopped: beyond max(3 steps, 5% of the window) from the edge
MEMO = 16
_QUANTILE_EXPR = re.compile(r"\b(histogram_quantile|quantile_over_time)\s*\(", re.IGNORECASE)

QUANTILE_REFUSAL = (
    "fleet refused on percentile series: the spread of member percentiles is not the fleet's "
    "percentile, and medians or bands of p99s aggregate percentiles (hint: compare members by a "
    "rate, or by the share of requests over a threshold per member with query_distribution + "
    "fraction_over, or by their histograms)"
)


def _r(v: float | None, sig: int = 4) -> float | None:
    if v is None or not math.isfinite(v):
        return None
    return float(f"{v:.{sig}g}")


def _arr(a: np.ndarray) -> list[float | None]:
    return [None if not math.isfinite(float(v)) else float(f"{float(v):.5g}") for v in a]


def member_names(labels: list[dict], by: list[str] | None) -> list[str]:
    """`by` labels if given, else the labels that vary across members."""
    keys = by or sorted({k for lb in labels for k in lb if len({x.get(k) for x in labels}) > 1})
    if not keys:
        return [f"#{i}" for i in range(len(labels))]
    return [",".join(f"{k}={lb.get(k, '')}" for k in keys) for lb in labels]


class FleetOps:
    def __init__(
        self,
        datasets: DatasetStore,
        signal: SignalOps,
        facts: Callable[[str, str], Facts],
    ) -> None:
        self._datasets = datasets
        self._signal = signal
        self._facts = facts
        self._memo: OrderedDict[tuple, tuple] = OrderedDict()
        self._last: dict[str, dict] = {}  # dataset -> options of the latest fleet() call

    def last_config(self, dataset_id: str) -> dict:
        """Options of the latest fleet() on this dataset; defaults when show() comes first."""
        return dict(self._last.get(dataset_id, {"by": None, "scale": "auto", "normalise": "none"}))

    # preconditions ------------------------------------------------------------------
    def check(self, dataset_id: str) -> DatasetMeta:
        meta = self._datasets.meta(dataset_id)
        if meta.representation == "quantile" or _QUANTILE_EXPR.search(meta.expr):
            raise ValueError(QUANTILE_REFUSAL)
        self._signal.check(dataset_id, "fleet")  # distributions, raw counters
        return meta

    # analysis -----------------------------------------------------------------------
    def run(
        self, dataset_id: str, by: list[str] | None, scale: str, normalise: str
    ) -> tuple[DatasetMeta, int, list[int], list[dict], list[str], Fleet, list[str]]:
        key = (dataset_id, tuple(by or ()), scale, normalise)
        if key in self._memo:
            self._memo.move_to_end(key)
            return self._memo[key]
        meta = self.check(dataset_id)
        meta, result = self._datasets.get(dataset_id)
        table, step = result.buckets, meta.step_ms
        caveats: list[str] = []
        if (meta.end_ms - meta.start_ms) // step + 1 > MAX_STEPS:
            table, step = lod(table, step, TimeRange(meta.start_ms, meta.end_ms), MAX_STEPS)
            caveats.append("coarsened")
        labels_of = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
        if any("quantile" in lb for lb in labels_of.values()):
            raise ValueError(QUANTILE_REFUSAL)
        ts = grid(meta.start_ms, meta.end_ms, step)
        index = {t: i for i, t in enumerate(ts)}
        df = pl.from_arrow(table)
        assert isinstance(df, pl.DataFrame)
        df = df.with_columns(pl.col("avg").fill_nan(None)).drop_nulls("avg")
        sids = list(labels_of)
        rows = {sid: r for r, sid in enumerate(sids)}
        y = np.full((len(sids), len(ts)), np.nan)
        for sid, t, v in zip(df["series_id"], df["ts_ms"], df["avg"], strict=True):
            j = index.get(int(t))
            if j is not None and sid in rows:
                y[rows[sid], j] = v
        labels = [labels_of[s] for s in sids]
        if len(sids) < MIN_MEMBERS:
            raise ValueError(
                f"{len(sids)} series: a fleet needs >= {MIN_MEMBERS} members (hint: show them as "
                "lines, or query without aggregating across the members, e.g. by (instance))"
            )
        if by:
            missing = [k for k in by if not any(k in lb for lb in labels)]
            if missing:
                raise ValueError(f"no member has label(s) {missing}: by must name member labels")
        names = member_names(labels, by)
        if len(set(names)) < len(names):
            dup = next(n for n in names if names.count(n) > 1)
            raise ValueError(
                f"by={by} does not identify members: several series share {dup} (hint: combine "
                f"them at the source, e.g. sum by ({', '.join(by or [])}) (...) for counts and "
                "rates; which aggregation is right depends on the metric)"
            )
        self._check_units(meta, labels)
        f = analyse(y, scale=scale, normalise=normalise)  # type: ignore[arg-type]
        res = (meta, step, ts, labels, names, f, caveats + f.caveats)
        self._memo[key] = res
        if len(self._memo) > MEMO:
            self._memo.popitem(last=False)
        return res

    def _check_units(self, meta: DatasetMeta, labels: list[dict]) -> None:
        names = sorted({lb["__name__"] for lb in labels if "__name__" in lb})
        if len(names) < 2:
            return
        units = {n: self._facts(meta.source, n).unit for n in names}
        known = {u for u in units.values() if u is not None}
        if len(known) > 1:
            raise ValueError(
                "fleet members must share a unit; these metrics disagree: "
                + ", ".join(f"{n} ({u or 'unknown'})" for n, u in units.items())
            )

    # summary for Claude -------------------------------------------------------------
    def summary(
        self,
        dataset_id: str,
        by: list[str] | None = None,
        scale: str = "auto",
        normalise: str = "none",
    ) -> dict:
        _, step, ts, labels, names, f, caveats = self.run(dataset_id, by, scale, normalise)
        self._last[dataset_id] = {"by": by, "scale": scale, "normalise": normalise}
        caveats = list(caveats)  # the memoised list stays as computed
        m_, t_ = f.z.shape
        sp = f.spread
        eff = format_duration(step)
        tol = max(3, math.ceil(CHURN_TOL * t_))
        appeared = [
            {"member": names[i], "first_seen": iso(ts[f.first_seen[i]])}
            for i in range(m_)
            if f.first_seen[i] >= tol
        ]
        stopped = [
            {"member": names[i], "last_seen": iso(ts[f.last_seen[i]])}
            for i in range(m_)
            if 0 <= f.last_seen[i] < t_ - tol
        ]
        never = sum(1 for i in range(m_) if f.first_seen[i] < 0)
        alive_sum = float(np.sum(sp.alive))
        missing = 1 - float(np.sum(sp.n)) / alive_sum if alive_sum else 0.0
        if missing > 0.05:
            caveats.append("missing_data")
        churn: dict = {"appeared": appeared[:MAX_LISTED], "stopped_reporting": stopped[:MAX_LISTED]}
        if len(appeared) > MAX_LISTED or len(stopped) > MAX_LISTED:
            churn["counts"] = {"appeared": len(appeared), "stopped_reporting": len(stopped)}
        if stopped:
            churn["note"] = (
                "a series that stops may have ended (replaced) or gone silent (the sick one): the "
                "source cannot tell; check the stopped members"
                + (" - as many appeared, likely replacement" if appeared and abs(len(appeared) - len(stopped)) <= 1 else "")
            )  # fmt: skip
        out = {
            "dataset": dataset_id,
            "effective_step": eff,
            "members": {
                "count": m_, "tested": len(f.tested), "untested": len(f.untested),
                "named_by": by or "labels that vary across members", "never_reported": never,
            },
            "scale": self._scale_text(f),
            "normalise": "none: members compared as they are" if f.normalise == "none"
            else "member: each member relative to its own median (shapes, not levels)",
            "spread": self._spread_summary(f, ts),
            "coverage": {
                "n_per_step": {"min": int(sp.n.min()), "median": float(np.median(sp.n)),
                               "max": int(sp.n.max())},
                "alive_max": int(sp.alive.max()), "missing_share": _r(missing, 3),
            },
            "churn": churn,
            "outliers": [self._outlier(dataset_id, eff, f, o, names, labels, ts)
                         for o in f.outliers[:MAX_LISTED]],
            "tests": self._tests(f),
            "caveats": caveats,
            "draw": f'show("{dataset_id}", question, mark="fleet")',
        }  # fmt: skip
        if len(f.outliers) > MAX_LISTED:
            out["more_outliers"] = len(f.outliers) - MAX_LISTED
        return out

    @staticmethod
    def _scale_text(f: Fleet) -> str:
        if f.scale == "log":
            return "log: members compared by ratio (all values > 0)"
        return "linear: members compared by difference"

    @staticmethod
    def _spread_summary(f: Fleet, ts: list[int]) -> dict:
        sp = f.spread
        with np.errstate(invalid="ignore", divide="ignore"):
            rel = sp.q75 / sp.q25 if f.scale == "log" else sp.q75 - sp.q25
        ok = np.isfinite(rel)
        if not ok.any():
            return {"basis": "fewer than 5 members per step: no quartiles"}
        third = max(1, len(ts) // 3)
        first, last = rel[:third], rel[-third:]
        widest = int(np.nanargmax(np.where(ok, rel, -np.inf)))
        kind = "q75 / q25 across members" if f.scale == "log" else "q75 - q25 across members"
        return {
            "basis": f"{kind} per step (descriptive: the fleet is the population)",
            "median": _r(float(np.nanmedian(rel))),
            "first_third": _r(float(np.nanmedian(first))) if np.isfinite(first).any() else None,
            "last_third": _r(float(np.nanmedian(last))) if np.isfinite(last).any() else None,
            "widest": {"at": iso(ts[widest]), "value": _r(float(rel[widest]))},
        }

    @staticmethod
    def _tests(f: Fleet) -> dict:
        th = f.thresholds
        heavy = [s for s in EXCURSIONS if th.get(f"{s}_heavy_tails")]
        out: dict = {
            "family_wise_alpha": ALPHA,
            "control": "Bonferroni over members x 3 tests (level, change, excursions; "
            "excursions also over member-steps and 3 durations)",
            "leave_one_out": f.loo,
            "members_tested": len(f.tested),
        }
        if "member" in th:
            out["member_threshold_z"] = _r(th["member"], 3)
            out["autocorrelation_time_steps"] = _r(th["tau"], 3)
            out["excursion_thresholds_z"] = {
                s: _r(th.get(f"{s}_tail_threshold", th[f"{s}_threshold"]), 3) for s in EXCURSIONS
            }
            out["excursion_widths_steps"] = dict(EXCURSIONS)
        if heavy:
            out["heavy_tailed_noise"] = (
                f"{', '.join(heavy)}: the fleet's noise exceeds normal-theory tails; those "
                "thresholds follow the other members' peaks (Gumbel)"
            )
        return out

    def _outlier(self, dataset_id, eff, f: Fleet, o: Outlier, names, labels, ts) -> dict:
        log = f.scale == "log"

        def eff_size(v: float, iv: tuple[float, float]) -> dict:
            if log:
                return {"ratio": _r(math.exp(v)), "interval_99": [_r(math.exp(iv[0])), _r(math.exp(iv[1]))]}  # fmt: skip
            return {"difference": _r(v), "interval_99": [_r(iv[0]), _r(iv[1])]}

        def evidence(name: str, v: float, iv: tuple[float, float], method: str, **params) -> dict:
            val, lo, hi = (math.exp(v), math.exp(iv[0]), math.exp(iv[1])) if log else (v, *iv)
            return {
                "kind": "statistic", "dataset": dataset_id, "name": name, "value": _r(val),
                "interval": [_r(lo), _r(hi)], "exact": False, "method": method,
                "params": {"member": names[o.member], "step": eff, "members": len(names),
                           "n": o.n, "tau": _r(o.tau, 3), "scale": f.scale, **params},
            }  # fmt: skip

        item: dict = {
            "member": names[o.member], "labels": labels[o.member], "kind": o.kind,
            "direction": o.direction, "score": _r(o.score, 3), "tests": o.fired,
            "z": {k: _r(v, 3) for k, v in o.z.items()},
            "since": iso(ts[o.since]) if o.since is not None else None,
            "since_window_start": o.since_window_start,
            "offset": eff_size(o.offset, o.offset_interval),
        }  # fmt: skip
        what = "ratio to the other members" if log else "difference from the other members"
        if o.kind == "persistent":
            item["evidence"] = evidence(
                "fleet_member_offset", o.offset, o.offset_interval,
                f"20% trimmed mean over the window of the member's {what} (per-step median of "
                "the others), 99% t interval from n_eff = n / tau",
            )  # fmt: skip
        elif o.kind in ("drifting", "shifted"):
            item["change"] = eff_size(o.change, o.change_interval)
            if o.at is not None:
                item["at"] = iso(ts[o.at])
            item["evidence"] = evidence(
                "fleet_member_change", o.change, o.change_interval,
                f"last third minus first third of the member's {what} (20% trimmed means), "
                "99% interval from n_eff",
            )  # fmt: skip
        else:
            eps = []
            for e in o.episodes[:5]:
                eps.append({"start": iso(ts[e.start]), "end": iso(ts[e.end]),
                            "steps": e.end - e.start + 1, "peak_z": _r(e.peak_z, 3),
                            "sustained": e.sustained})  # fmt: skip
            item["episodes"] = eps
            if len(o.episodes) > 5:
                item["more_episodes"] = len(o.episodes) - 5
            main = max(o.episodes, key=lambda e: abs(e.peak_z))
            dev = f.d[o.member, main.start : main.end + 1]
            dev = dev[np.isfinite(dev)]
            if dev.size >= 3:
                v, iv = trimmed_interval(dev, o.tau)
                item["evidence"] = evidence(
                    "fleet_member_excursion", v, iv,
                    f"20% trimmed mean of the member's {what} during its strongest episode, "
                    "99% interval from n_eff",
                    start=iso(ts[main.start]), end=iso(ts[main.end]), peak_z=_r(main.peak_z, 3),
                )  # fmt: skip
        return item

    # panel payload --------------------------------------------------------------------
    def panel(self, dataset_id: str, cfg: dict) -> dict:
        _, step, ts, labels, names, f, caveats = self.run(
            dataset_id, cfg.get("by"), cfg.get("scale", "auto"), cfg.get("normalise", "none")
        )
        caveats = list(caveats)
        sp = f.spread
        drawn = []
        for o in f.outliers[:MAX_DRAWN]:
            drawn.append({
                "id": names[o.member], "labels": labels[o.member], "kind": o.kind,
                "direction": o.direction, "score": _r(o.score, 3),
                "values": _arr(f.values[o.member]),
                "since_ms": ts[o.since] if o.since is not None else None,
                "episodes": [[ts[e.start], ts[e.end]] for e in o.episodes],
            })  # fmt: skip
        return {
            "kind": "fleet",
            "effective_step_ms": step,
            "ts": ts,
            "members": len(names),
            "normalise": f.normalise,
            "scale": f.scale,
            "band": {
                k: _arr(getattr(sp, k)) for k in ("median", "q25", "q75", "q10", "q90", "lo", "hi")
            },
            "n": [int(v) for v in sp.n],
            "alive": [int(v) for v in sp.alive],
            "outliers": drawn,
            "outlier_count": len(f.outliers),
            "caveats": caveats,
        }
