"""Periodicity and filter ops over time-series datasets (beads 4ok.7, 4ok.8, 4ok.9).

Preconditions (percentiles, distributions, raw counters) are refused with hints; nothing is
interpolated; results carry intervals and an `evidence` statistic ready for finding_create."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import polars as pl
import pyarrow.compute as pc

from telemetry_nerd.analysis import born_counters
from telemetry_nerd.analysis.born_counters import BornCounter, Filled
from telemetry_nerd.analysis.filters import FilterSpec, filter_buckets
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.analysis.spectrum import (
    MAX_GAP_FRACTION,
    MIN_POINTS,
    Spectrum,
    log_bins,
    spectrogram,
    spectrum,
)
from telemetry_nerd.analysis.timeops import time_op_problem
from telemetry_nerd.catalog.rules import Facts
from telemetry_nerd.charts.dataview import offered_views
from telemetry_nerd.charts.units import nonaggregatable_metrics, raw_counters
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.series import FetchResult
from telemetry_nerd.model.time import TimeRange, format_duration

SPECTRUM_CAP = 4096
SERIES_BUDGET = 12
MEMO = 32


@dataclass
class Prepared:
    meta: DatasetMeta
    step_ms: int
    caveats: list[str]
    series: dict[str, tuple[dict, np.ndarray, np.ndarray]]  # sid -> (labels, ts_ms, y)
    skipped: list[dict]
    #: the born-on-first-event reading of the expression (None: absence is missing data)
    born: BornCounter | None = None
    #: per series, the steps read as 0 events from a live sibling (born_counters)
    absent_zero: dict[str, Filled] | None = None


def human_period(seconds: float) -> str:
    """3638 -> "1h", 10530 -> "2.9h": periods are rarely round durations."""
    for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60)):
        if seconds >= size:
            return f"{float(f'{seconds / size:.3g}'):g}{unit}"
    return f"{float(f'{seconds:.3g}'):g}s"


def _labels(series_table) -> dict[str, dict]:
    import json

    return {r["series_id"]: json.loads(r["labels"]) for r in series_table.to_pylist()}


def same_grid(a: DatasetMeta, b: DatasetMeta) -> bool:
    """`a` covers `b`'s range on the same step grid (same step and phase)."""
    return (
        a.source == b.source
        and a.step_ms == b.step_ms
        and (a.start_ms - b.start_ms) % b.step_ms == 0
        and a.start_ms <= b.start_ms
        and a.end_ms >= b.end_ms
    )


def _grouped(table, labels: dict[str, dict]) -> dict[str, tuple[dict, np.ndarray, np.ndarray]]:
    """sid -> (labels, ts_ms, y) of the steps with a finite value, sorted by time."""
    df = pl.from_arrow(table).with_columns(pl.col("avg").fill_nan(None)).drop_nulls("avg")
    out: dict[str, tuple[dict, np.ndarray, np.ndarray]] = {}
    for (sid,), g in df.sort("ts_ms").group_by("series_id", maintain_order=True):
        out[sid] = (labels.get(sid, {}), g["ts_ms"].to_numpy(), g["avg"].to_numpy())
    return out


class SignalOps:
    def __init__(self, datasets: DatasetStore, facts: Callable[[str, str], Facts]) -> None:
        self._datasets = datasets
        self._facts = facts
        self._memo: OrderedDict[tuple, tuple[Prepared, dict[str, Spectrum]]] = OrderedDict()

    @property
    def datasets(self) -> DatasetStore:
        return self._datasets

    # preconditions ------------------------------------------------------
    def check(self, dataset_id: str, op: str) -> DatasetMeta:
        meta = self._datasets.meta(dataset_id)
        # a code output's expr names an output, not catalog metrics
        facts = lambda m: self._facts(meta.source, m)
        counters = [] if meta.code_node else raw_counters(meta.expr, facts)
        flagged = [] if meta.code_node else nonaggregatable_metrics(meta.expr, facts)
        problem = time_op_problem(op, meta.representation, counters, flagged)
        if problem:
            raise ValueError(problem)
        return meta

    def born(self, dataset_id: str) -> BornCounter | None:
        """The born-on-first-event reading of a source dataset's expression, if any."""
        meta = self._datasets.meta(dataset_id)
        if meta.code_node or meta.derived:
            return None
        return born_counters.born_counter(meta.expr, lambda m: self._facts(meta.source, m).type)

    def _prepare(
        self,
        dataset_id: str,
        op: str,
        cap: int,
        allow_empty: bool = False,
        sibling: str | None = None,
    ) -> Prepared:
        """Series that qualify for `op`. `sibling`: a dataset of the live-sibling expression
        (born_counters complement), same range and step, fetched by the caller."""
        meta = self.check(dataset_id, op)
        meta, result = self._datasets.get(dataset_id)
        table, step = result.buckets, meta.step_ms
        caveats: list[str] = []
        span = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
        rng = TimeRange(meta.start_ms, meta.end_ms)
        if span > cap:
            table, step = lod(result.buckets, meta.step_ms, rng, cap)
            caveats.append("coarsened")
        labels = _labels(result.series)
        raw = _grouped(table, labels)
        born = self.born(dataset_id)
        filled: dict[str, Filled] = {}
        if born is not None:
            siblings = None
            if sibling is not None:
                smeta, sres = self._datasets.get(sibling)
                if not same_grid(smeta, meta):
                    raise ValueError(f"sibling {sibling} does not share {dataset_id}'s step grid")
                ts_col = pc.field("ts_ms")
                stable = sres.buckets.filter(
                    (ts_col >= meta.start_ms) & (ts_col <= meta.end_ms)
                )  # never fill outside the dataset's range
                if step != meta.step_ms:
                    stable = lod(stable, meta.step_ms, rng, cap)[0]
                siblings = {
                    sid: (lab, ts)
                    for sid, (lab, ts, _) in _grouped(stable, _labels(sres.series)).items()
                }
            raw, filled = born_counters.fill_born(raw, siblings)
            if filled:
                caveats.append(born_counters.CAVEAT)
        series: dict[str, tuple[dict, np.ndarray, np.ndarray]] = {}
        skipped: list[dict] = []
        expected = (meta.end_ms - meta.start_ms) // step + 1
        gappy = False
        for sid, (lab, ts, y) in raw.items():
            gaps = 1 - ts.size / max(expected, 1)
            reason = None
            if ts.size < MIN_POINTS:
                reason = "too_few_points"
            elif gaps > MAX_GAP_FRACTION:
                reason = "too_gappy"
            elif float(np.ptp(y)) == 0:
                reason = "constant"
            if reason:
                item = {"labels": lab, "reason": reason}
                if (
                    born is not None
                    and born.complement
                    and sibling is None
                    and reason != "constant"
                ):
                    item["hint"] = (
                        "a counter series born on its first event: absence may mean 0 events; "
                        f"analyze checks it against the live sibling {born.complement}"
                    )
                skipped.append(item)
            else:
                series[sid] = (lab, ts, y)
                gappy = gappy or gaps > 0.2
        if gappy:
            caveats.append("gaps")
        if skipped:
            caveats.append("skipped_series")
        if not series and not allow_empty:
            raise ValueError(
                f"{op}: no series qualifies ({', '.join(sorted({s['reason'] for s in skipped})) or 'no data'}); "
                f"needs >= {MIN_POINTS} points, <= {int(MAX_GAP_FRACTION * 100)}% gaps, not constant"
            )
        if len(series) > SERIES_BUDGET:
            raise ValueError(
                f"{len(series)} series: at most {SERIES_BUDGET} (hint: aggregate first, e.g. sum by (...))"
            )
        return Prepared(
            meta, step, caveats, series, skipped, born,
            {sid: f for sid, f in filled.items() if sid in series},
        )  # fmt: skip

    # spectrum -----------------------------------------------------------
    def spectrum_of(
        self, dataset_id: str, min_period_ms: int | None = None, max_period_ms: int | None = None
    ) -> tuple[Prepared, dict[str, Spectrum]]:
        key = (dataset_id, min_period_ms, max_period_ms)
        if key in self._memo:
            self._memo.move_to_end(key)
            return self._memo[key]
        prep = self._prepare(dataset_id, "spectrum", SPECTRUM_CAP)
        spectra = {
            sid: spectrum(
                ts, y, prep.step_ms, top=8, min_period_ms=min_period_ms, max_period_ms=max_period_ms
            )
            for sid, (_, ts, y) in prep.series.items()
        }
        self._memo[key] = (prep, spectra)
        if len(self._memo) > MEMO:
            self._memo.popitem(last=False)
        return prep, spectra

    def spectrum_summary(
        self,
        dataset_id: str,
        top: int = 3,
        min_period_ms: int | None = None,
        max_period_ms: int | None = None,
    ) -> dict:
        prep, spectra = self.spectrum_of(dataset_id, min_period_ms, max_period_ms)
        eff = format_duration(prep.step_ms)
        caveats = list(prep.caveats)
        out = []
        for sid, sp in spectra.items():
            labels = prep.series[sid][0]
            peaks = []
            for pk in sp.peaks[:top]:
                p_s = float(pk.period_ms / 1000)
                item = {
                    "period": human_period(p_s),
                    "period_s": float(f"{p_s:.4g}"),
                    "interval_s": [
                        float(f"{pk.lo_ms / 1000:.4g}"),
                        float(f"{pk.hi_ms / 1000:.4g}"),
                    ],
                    "power": float(f"{pk.power:.3g}"),
                    "fap": float(f"{pk.fap:.2g}"),
                    "fap_red_noise": float(f"{pk.fap_red_noise:.2g}"),
                    "local_ratio": None
                    if pk.local_ratio is None
                    else float(f"{pk.local_ratio:.3g}"),
                    "significant": bool(pk.significant),
                    "sampling_artifact": bool(pk.window > 0.1),
                }
                if pk.significant:
                    item["evidence"] = {
                        "kind": "statistic", "dataset": dataset_id, "name": "dominant_period",
                        "value": item["period_s"], "interval": item["interval_s"], "exact": False,
                        "method": "Lomb-Scargle peak, half-power width (>= 1/range); "
                                  "confirmed against AR(1) red noise",
                        "params": {
                            "fap": pk.fap, "fap_red_noise": pk.fap_red_noise,
                            "ar1_phi": round(sp.phi, 3), "local_ratio": pk.local_ratio,
                            "n": sp.n, "step": eff, "detrended": "linear",
                        },
                    }  # fmt: skip
                peaks.append(item)
            for c in sp.caveats:
                if c not in caveats:
                    caveats.append(c)
            out.append({"labels": labels, "n": sp.n, "peaks": peaks})
        first = next(iter(spectra.values()))
        return {
            "dataset": dataset_id,
            "effective_step": eff,
            "limits": {
                "shortest": human_period(first.shortest_ms / 1000),
                "longest": human_period(first.longest_ms / 1000),
            },
            "series": out,
            "skipped": prep.skipped,
            "caveats": caveats,
        }

    # filter -------------------------------------------------------------
    def filter(
        self, dataset_id: str, spec: FilterSpec, reason: str
    ) -> tuple[DatasetMeta, FetchResult, dict]:
        reason = (reason or "").strip()
        if not reason or "\n" in reason or len(reason) > 160:
            raise ValueError("reason is required: one line of at most 160 characters")
        meta = self.check(dataset_id, "filter")
        if meta.derived:
            raise ValueError(
                f"{dataset_id} is already filtered (hint: filter its source {meta.derived['from']}; "
                "use band-pass for two cutoffs)"
            )
        meta, result = self._datasets.get(dataset_id)
        span = meta.end_ms - meta.start_ms + meta.step_ms
        warnings = spec.check(meta.step_ms, span)
        out = filter_buckets(spec, result.buckets, meta.step_ms)
        caveats = list(warnings)
        if out.edge_share > 0:
            caveats.append("filter_edges")
        if out.edge_share > 0.5:
            caveats.append("mostly_edge")
        derived = {
            "op": spec.kind,
            "from": dataset_id,
            "label": spec.label(),
            "reason": reason,
            "period_ms": spec.period_ms,
            "period_hi_ms": spec.period_hi_ms,
            "edges": out.edges,
        }
        summary = {
            "filter": spec.label(),
            "removed_share": {sid: float(f"{v:.3g}") for sid, v in out.removed_share.items()},
            "edge_share": float(f"{out.edge_share:.3g}"),
            "views": offered_views(spec.kind),
            "default_view": offered_views(spec.kind)[0],
            "caveats": caveats,
        }
        return (
            meta,
            FetchResult(out.buckets, result.series),
            {"derived": derived, "summary": summary},
        )

    # panel payloads ------------------------------------------------------
    def spectrum_panel(
        self, dataset_id: str, min_period_ms: int | None, max_period_ms: int | None, width_px: int
    ) -> dict:
        prep, spectra = self.spectrum_of(dataset_id, min_period_ms, max_period_ms)
        summary = self.spectrum_summary(dataset_id, 5, min_period_ms, max_period_ms)
        n_bins = max(32, width_px // 2)
        series = []
        for (sid, sp), info in zip(spectra.items(), summary["series"], strict=True):
            periods = 1000.0 / sp.freqs  # ms, descending frequency -> ascending period
            edges, power = log_bins(periods, sp.power, n_bins)
            centres = np.sqrt(edges[:-1] * edges[1:]) / 1000.0
            ok = ~np.isnan(power)
            red = sp.red_level(1.0 / centres[ok], prep.step_ms / 1000)
            series.append(
                {
                    "id": sid,
                    "labels": prep.series[sid][0],
                    "periods_s": [float(f"{x:.5g}") for x in centres[ok]],
                    "power": [float(f"{x:.4g}") for x in power[ok]],
                    "level": float(f"{sp.level:.4g}"),
                    "red_level": [float(f"{x:.4g}") for x in red],
                    "ar1_phi": float(f"{sp.phi:.3g}"),
                    "peaks": info["peaks"],
                    "caveats": sp.caveats,
                }
            )
        first = next(iter(spectra.values()))
        return {
            "kind": "spectrum",
            "effective_step_ms": prep.step_ms,
            "limits": {
                "shortest_s": first.shortest_ms / 1000,
                "longest_s": first.longest_ms / 1000,
            },
            "series": series,
            "skipped": prep.skipped,
            "caveats": summary["caveats"],
        }

    def spectrogram_panel(
        self, dataset_id: str, segment_ms: int, overlap: float, height_px: int
    ) -> dict:
        prep = self._prepare(dataset_id, "spectrogram", 8192)
        n_rows = max(8, height_px // 4)
        series = []
        hop = 0
        for sid, (labels, ts, y) in prep.series.items():
            sg = spectrogram(ts, y, prep.step_ms, segment_ms=segment_ms, overlap=overlap)
            if sg.centres_ms.size > 400:
                raise ValueError(
                    f"{sg.centres_ms.size} spectrogram columns (max 400): use a longer segment or less overlap"
                )
            hop = sg.hop_ms
            periods = 1000.0 / sg.freqs
            edges, rows = log_bins(periods, sg.power.T, n_rows)  # rows: (n_rows, cols)
            lo_s, hi_s = edges[:-1] / 1000.0, edges[1:] / 1000.0
            keep = ~np.all(np.isnan(rows), axis=1)
            power = [
                [None if np.isnan(v) else float(f"{v:.3g}") for v in rows[k]]
                for k in np.flatnonzero(keep)
            ]
            series.append(
                {
                    "id": sid,
                    "labels": labels,
                    "ts": [int(t) for t in sg.centres_ms],
                    "rows": {
                        "lo_s": [float(f"{x:.5g}") for x in lo_s[keep]],
                        "hi_s": [float(f"{x:.5g}") for x in hi_s[keep]],
                    },
                    "power": power,  # [row][column]
                    "level": [None if np.isnan(v) else float(f"{v:.3g}") for v in sg.level],
                }
            )
        return {
            "kind": "spectrogram",
            "segment_ms": segment_ms,
            "hop_ms": hop,
            "overlap": overlap,
            "effective_step_ms": prep.step_ms,
            "limits": {"shortest_s": 2 * prep.step_ms / 1000, "longest_s": segment_ms / 2000},
            "series": series,
            "skipped": prep.skipped,
            "caveats": prep.caveats,
        }
