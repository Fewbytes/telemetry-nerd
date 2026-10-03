"""analyze: one-call series diagnostics for Claude and the SPC panel (bead lkn.1).

Preconditions, coarsening and gap rules come from SignalOps._prepare (as for spectrum). The
baseline window is stated, defaults to the first half of the range, and is the ONLY data the
control limits see. Every headline number carries an interval and an `evidence` statistic.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import numpy as np

from telemetry_nerd.analysis import sources
from telemetry_nerd.analysis.diagnostics import (
    Diagnosis,
    detector_source,
    diagnose,
    violation_sources,
)
from telemetry_nerd.analysis.profile import seasonal_shape
from telemetry_nerd.analysis.seasonal import DEFAULT_K, cycle_shifts
from telemetry_nerd.analysis.sources import COMMON, SPECIAL
from telemetry_nerd.analysis.spc import CUSUM_H, CUSUM_K, EWMA_L, EWMA_LAMBDA
from telemetry_nerd.analysis.spectrum import spectrum
from telemetry_nerd.core.profiles import SeasonalShapes
from telemetry_nerd.core.signal_ops import SPECTRUM_CAP, Prepared, SignalOps, human_period
from telemetry_nerd.core.wire import (
    Memo,
    add_caveats,
    measurement_caveats,
    sig,
    sig_pair,
    statistic,
)
from telemetry_nerd.datasets.store import DatasetMeta
from telemetry_nerd.model.time import format_duration, iso

MAX_VIOLATIONS_LISTED = 5
GAP_HANDLING = (
    "gaps are never filled: run rules break at a gap; across g missing steps the EWMA decays "
    "by (1-lambda)^g and each CUSUM side drains by g*k, so a long gap restarts them "
    "(conservative: the in-control ARL can only grow)"
)
#: (source, expr, exclude_start_ms, exclude_end_ms) -> the operating profile's seasonal models
#: re-fitted without that span, or None
ShapeLookup = Callable[[str, str, int, int], SeasonalShapes | None]
#: analyze(baseline=...) -> the cycle scheme of seasonal comparison (lkn.2) whose alignment it reuses
REFERENCE_SCHEMES = {"previous": "previous", "day": "1d", "week": "1w"}
Run = tuple[Prepared, tuple[int, int, str], dict[str, Diagnosis], dict[str, dict]]


def default_baseline(meta: DatasetMeta) -> tuple[int, int]:
    """First half of the dataset range, on whole steps: [start, end)."""
    steps = (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    return meta.start_ms, meta.start_ms + (steps // 2) * meta.step_ms


def resolve_baseline(
    meta: DatasetMeta, start_ms: int | None, end_ms: int | None
) -> tuple[int, int, str]:
    if start_ms is None and end_ms is None:
        s, e = default_baseline(meta)
        return s, e, "first half of the range (default)"
    s = meta.start_ms if start_ms is None else start_ms
    e = meta.end_ms + meta.step_ms if end_ms is None else end_ms
    if not meta.start_ms - meta.step_ms <= s < e <= meta.end_ms + meta.step_ms:
        raise ValueError(
            f"baseline {iso(s)}..{iso(e)} must lie inside the dataset range "
            f"{iso(meta.start_ms)}..{iso(meta.end_ms)} (hint: pick a calm stretch before the "
            "period you want to judge)"
        )
    return s, e, "stated"


def reference_baseline(meta: DatasetMeta, ref: dict) -> tuple[int, int, str]:
    """[start, end) spanning every reference window, and its stated label."""
    span = meta.end_ms - meta.start_ms + meta.step_ms
    shifts = [r["shift_ms"] for r in ref["refs"]]
    return meta.start_ms - max(shifts), meta.start_ms - min(shifts) + span, ref["label"]


def reference_windows(meta: DatasetMeta, ref: dict) -> list[dict]:
    span = meta.end_ms - meta.start_ms + meta.step_ms
    return [
        {"start": iso(meta.start_ms - r["shift_ms"]), "end": iso(meta.start_ms - r["shift_ms"] + span),
         "dataset": r["dataset"]}
        for r in sorted(ref["refs"], key=lambda r: -r["shift_ms"])
    ]  # fmt: skip


def reference_label(baseline: str, k: int, span_ms: int, tz: str) -> str:
    span = format_duration(span_ms)
    if baseline == "previous":
        if k == 1:
            return f"the preceding {span} window (fetched separately)"
        return f"the {k} preceding {span} windows (fetched separately)"
    unit = "day" if baseline == "day" else "week"
    where = "UTC" if tz == "UTC" else f"local time {tz}"
    days = f"previous {unit}" if k == 1 else f"previous {k} {unit}s"
    return f"the same {span} window on the {days}, aligned by {where} (fetched separately)"


class SeriesDiagnostics:
    def __init__(
        self,
        signal: SignalOps,
        shapes: ShapeLookup | None = None,
        query: Callable[..., Awaitable[dict]] | None = None,
    ) -> None:
        self._signal = signal
        self._shapes = shapes
        self._query = query
        self._memo: Memo[Run] = Memo()
        self._last: dict[str, dict | None] = {}  # dataset -> reference config of the last analyze

    async def fetch_reference(
        self, dataset_id: str, baseline: str, cycles: int, tz: str, actor: str
    ) -> dict:
        """Fetch the reference baseline windows (same expr, step, source) through the cache.

        Alignment is compare_seasonal's (`cycle_shifts`): `previous` = the preceding windows,
        `day` / `week` = the same window on previous local calendar days / weeks in `tz`."""
        if baseline not in REFERENCE_SCHEMES:
            raise ValueError(
                f"unknown baseline {baseline!r}: use window (default), "
                + ", ".join(REFERENCE_SCHEMES)
            )
        scheme = REFERENCE_SCHEMES[baseline]
        if not 1 <= cycles <= DEFAULT_K[scheme]:
            raise ValueError(f"baseline_cycles for {baseline} must be 1..{DEFAULT_K[scheme]}")
        meta = self._signal.check(dataset_id, "analyze")
        if meta.derived:
            raise ValueError(
                f"{dataset_id} is filtered; analyze the raw series against a reference "
                f"(hint: analyze({meta.derived['from']}, baseline={baseline!r}))"
            )
        if self._query is None:
            raise ValueError("reference baselines need a source to fetch from")
        span = meta.end_ms - meta.start_ms + meta.step_ms
        shifts = cycle_shifts(meta.start_ms, scheme, cycles, tz, span, meta.step_ms)
        refs = []
        for j, sh in enumerate(shifts, 1):
            out = await self._query(
                meta.expr,
                start=str(meta.start_ms - sh),
                end=str(meta.end_ms - sh),
                step=format_duration(meta.step_ms),
                source=meta.source,
                actor=actor,
            )
            refs.append({"j": j, "shift_ms": sh, "dataset": out["dataset"]})
        return {
            "baseline": baseline, "tz": tz, "cycles": cycles, "refs": refs,
            "label": reference_label(baseline, cycles, span, tz),
        }  # fmt: skip

    def remember(self, dataset_id: str, ref: dict | None) -> None:
        self._last[dataset_id] = ref

    def last_reference(self, dataset_id: str) -> dict | None:
        return self._last.get(dataset_id)

    def run(
        self,
        dataset_id: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        ref: dict | None = None,
    ) -> Run:
        """(prepared series, baseline (start, end, basis), diagnosis per series, per series the
        operating profile whose seasonal shape is the SPC centre)."""
        meta = self._signal.check(dataset_id, "analyze")
        base = reference_baseline(meta, ref) if ref else resolve_baseline(meta, start_ms, end_ms)
        # the seasonal shape never sees the data being judged: the whole dataset is excluded
        shapes = (
            self._shapes(meta.source, meta.expr, meta.start_ms - meta.step_ms, meta.end_ms)
            if self._shapes
            else None
        )
        key = (
            dataset_id, start_ms, end_ms, json.dumps(ref, sort_keys=True) if ref else None,
            (shapes.profile_id, shapes.computed_at_ms) if shapes else None,
        )  # fmt: skip
        if (hit := self._memo.get(key)) is not None:
            return hit
        prep = self._signal._prepare(dataset_id, "analyze", SPECTRUM_CAP, allow_empty=True)
        refs = [
            self._signal._prepare(r["dataset"], "analyze", SPECTRUM_CAP, allow_empty=True)
            for r in sorted(ref["refs"], key=lambda r: -r["shift_ms"])
        ] if ref else []  # fmt: skip
        out: dict[str, Diagnosis] = {}
        used: dict[str, dict] = {}
        for sid, (labels, ts, y) in prep.series.items():
            try:
                sp = spectrum(ts, y, prep.step_ms, top=8)
            except ValueError:
                sp = None
            mask = (ts >= base[0]) & (ts < base[1])
            reference = None
            if ref:
                parts = [r.series[sid] for r in refs if sid in r.series and r.step_ms == prep.step_ms]  # fmt: skip
                reference = (
                    np.concatenate([p[1] for p in parts]) if parts else np.zeros(0, np.int64),
                    np.concatenate([p[2] for p in parts]) if parts else np.zeros(0),
                )
            profile = None
            seas = shapes.for_labels(labels) if shapes else None
            if seas is not None and shapes is not None:
                at = np.r_[reference[0], ts] if reference is not None else ts
                profile = (seasonal_shape(seas, at - prep.step_ms // 2), shapes.cycle_s(seas))
            d = diagnose(
                ts, y, prep.step_ms, mask, sp, lambda v: f"{v:.3g}", iso, reference, profile
            )
            out[sid] = d
            if d.chart is not None and "profile" in d.chart.seasonal and shapes and seas:
                used[sid] = {
                    "profile": shapes.profile_id, "expr": shapes.expr, "model": seas.period,
                    "history": format_duration(shapes.history_ms),
                    "judged_hours_excluded": shapes.excluded_hours,
                }  # fmt: skip
        res = (prep, base, out, used)
        self._memo.put(key, res)
        return res

    # summary for Claude ------------------------------------------------------
    def summary(
        self,
        dataset_id: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        ref: dict | None = None,
    ) -> dict:
        prep, base, diags, used = self.run(dataset_id, start_ms, end_ms, ref)
        eff = format_duration(prep.step_ms)
        caveats = list(prep.caveats)
        series = []
        expected = (prep.meta.end_ms - prep.meta.start_ms) // prep.step_ms + 1
        for sid, d in diags.items():
            labels, ts, _ = prep.series[sid]
            item = self._series_summary(dataset_id, eff, base, labels, ts, d, used.get(sid))
            if (missing := expected - int(ts.size)) > 0:
                item["variation"].append(sources.item(
                    sources.MEASUREMENT, f"{missing} of {expected} steps without data (never "
                    "filled: run rules break there, EWMA/CUSUM decay across them)",
                    missing_steps=missing,
                ))  # fmt: skip
            series.append(item)
            add_caveats(caveats, d.caveats)
        for sk in prep.skipped:
            series.append({
                "labels": sk["labels"], "verdict": "insufficient_data",
                "reasons": [f"skipped: {sk['reason']}"],
            })  # fmt: skip
        baseline: dict = {"start": iso(base[0]), "end": iso(base[1]), "basis": base[2]}
        if ref:
            baseline |= {
                "kind": "reference",
                "windows": reference_windows(prep.meta, ref),
                "note": "fetched separately; every point of the dataset is judged",
            }
        return {
            "dataset": dataset_id,
            "effective_step": eff,
            "baseline": baseline,
            "series": series,
            "caveats": caveats,
            # spec §5.4: dataset-wide measurement-system items; per series in series[].variation
            "variation": sources.measurement_items(prep.caveats + measurement_caveats(prep.meta)),
            "draw": f'show("{dataset_id}", question, mark="spc"'
            + ("" if base[2] != "stated" else ", windows=[the baseline]")
            + ")"
            + (" (uses this reference baseline)" if ref else ""),
        }

    @staticmethod
    def _series_summary(dataset_id, eff, base, labels, ts, d: Diagnosis, profile=None) -> dict:
        def ev(name, value, interval, method, source=None, **params):
            return statistic(
                dataset_id, name, sig(value), sig_pair(interval), method,
                {"step": eff, "n": d.n, "n_eff": sig(d.n_eff, 3), **params}, source=source,
            )  # fmt: skip

        out: dict = {
            "labels": labels,
            "verdict": d.verdict,
            "also": d.also,
            "reasons": d.reasons,
            "n": d.n,
            # spec §5.4: each finding labelled common cause / special cause / measurement
            # system / undetermined (measurement items from this series' caveats)
            "variation": d.variation + sources.measurement_items(d.caveats),
        }
        if d.trend is None:  # too few points: nothing else was computed
            return out
        out |= {
            "n_eff": sig(d.n_eff, 3),
            "autocorrelation_time_steps": sig(d.tau, 3),
            "structure": d.model,
        }
        periods = []
        for pk, pr in zip(d.peaks, d.periods, strict=True):
            p_s = pk.period_ms / 1000
            interval = [sig(pk.lo_ms / 1000), sig(pk.hi_ms / 1000)]
            periods.append({
                "period": human_period(p_s), "period_s": sig(p_s), "interval_s": interval,
                "power": sig(pr.power, 3), "fap_red_noise": sig(pr.fap, 2),
                "evidence": ev(
                    "dominant_period", p_s, interval,
                    "Lomb-Scargle peak, half-power width; confirmed against AR(1) red noise",
                    source=COMMON, fap=pr.fap, phi_background=sig(pr.phi, 3),
                ),
            })  # fmt: skip
        out["frequency"] = {
            "periods": periods,
            "white_noise_candidates_rejected": len(d.candidates) - len(d.peaks),
        }
        tr = d.trend
        trend = {
            "change_over_range": sig(tr.change), "interval": sig_pair(tr.change_interval),
            "per_hour": sig(tr.slope_per_h), "significant": tr.significant,
        }  # fmt: skip
        if "drifting" in [d.verdict, *d.also]:
            trend["evidence"] = ev(
                "trend_change_over_range", tr.change, tr.change_interval,
                "OLS slope x range, SE inflated by sqrt(tau) of residuals, 99% t interval",
                source=SPECIAL,
            )  # fmt: skip
        shifts = []
        for s in d.shifts:
            item = {
                "at": iso(s.ts_ms), "delta": sig(s.delta), "interval": sig_pair(s.interval),
                "sigma_units": sig(abs(s.delta) / d.sigma_within, 3), "p": sig(s.p, 2),
                "n_before": s.n_before, "n_after": s.n_after, "source": SPECIAL,
            }  # fmt: skip
            if d.model == "step":
                item["evidence"] = ev(
                    "level_shift", s.delta, s.interval,
                    "CUSUM changepoint (Kolmogorov null, AR(1) long-run sigma), 99% interval",
                    source=SPECIAL, at=iso(s.ts_ms), p=s.p,
                )  # fmt: skip
            shifts.append(item)
        stability = {"trend": trend, "shifts": shifts, "sigma_within": sig(d.sigma_within)}
        if d.kpss is not None:
            stability["kpss"] = {
                "stat": sig(d.kpss.stat, 3),
                "p": "> 0.1" if d.kpss.p_upper >= 0.1 else f"<= {d.kpss.p_upper:g}",
                "lags": d.kpss.lags,
            }
        if d.variance is not None:
            stability["variance_ratio_last_first_third"] = {
                "value": sig(d.variance.ratio, 3), "interval": sig_pair(d.variance.interval),
            }  # fmt: skip
        out["stability"] = stability
        out["spc"] = SeriesDiagnostics._spc_summary(d, base, ts, ev, profile)
        sh = d.shape
        if sh is not None:
            out["shape"] = {
                "skew": [sig(sh.skew, 3), *sig_pair(sh.skew_interval)],
                "excess_kurtosis": [sig(sh.excess_kurtosis, 3), *sig_pair(sh.kurtosis_interval)],
                "zeros": sh.zeros, "zero_share_interval": sig_pair(sh.zero_share_interval),
                "bimodality_coefficient": sig(sh.bimodality, 3), "flags": sh.flags,
            }  # fmt: skip
        if d.caveats:
            out["caveats"] = d.caveats
        return out

    @staticmethod
    def _spc_summary(d: Diagnosis, base, ts, ev, profile=None) -> dict:
        c = d.chart
        if c is None or c.mode == "insufficient_data":
            return {"mode": "insufficient_data", "reason": c.reason if c else None}
        bwin = [iso(base[0]), iso(base[1])]
        nb = {"n": c.n_baseline, "n_eff": sig(c.n_eff_baseline, 3)}  # not the series'
        # level + seasonal: the centre at rest (a separate reference is not in c.centre)
        level = float(np.nanmedian(c.centre[c.baseline])) if c.baseline.any() else c.level
        detectors = {
            name: {
                "count": det.count, "of": det.opportunities, "expected": sig(det.expected, 3),
                **({"p": sig(det.p, 2)} if det.p is not None else {}),
                **({"source": src} if (src := detector_source(c, name)) else {}),
            }
            for name, det in c.detectors.items()
        }  # fmt: skip
        viol = c.violations()
        vsrc = violation_sources(c, d.shifted_from)
        first = [
            {"t": iso(int(ts[i])), "rules": rules, "source": vsrc[i]}
            for i, rules in list(viol.items())[:MAX_VIOLATIONS_LISTED]
        ]
        return {
            "mode": c.mode,
            "baseline": nb,
            "centre": {
                "value": sig(level), "interval": sig_pair(c.centre_interval),
                "seasonal_periods_s": [sig(p) for p in c.seasonal_periods_s],
                "seasonal": c.seasonal,
                **({"seasonal_profile": profile} if profile else {}),
                "evidence": ev(
                    "spc_centre_line", level, c.centre_interval,
                    "baseline median" + (
                        " of y minus the operating profile's seasonal shape (re-fitted without "
                        "the judged hours)" if profile else ""
                    ) + ", 99% interval from n_eff; centre of the common-cause envelope",
                    source=COMMON, baseline=bwin, **nb,
                ),
            },
            "sigma": {
                "value": sig(c.sigma), "interval": sig_pair(c.sigma_interval),
                "evidence": ev(
                    "spc_sigma", c.sigma, c.sigma_interval,
                    "1.4826 MAD of baseline deviations (marginal), 99% interval from n_eff; the "
                    "common-cause scale", source=COMMON, baseline=bwin, **nb,
                ),
            },
            "limits_3sigma": [sig(level - 3 * c.sigma), sig(level + 3 * c.sigma)],
            "envelope": {"source": COMMON, "meaning": sources.MEANING[COMMON]},
            "lag1_phi": sig(c.phi, 3),
            "in_control": c.in_control,
            "outside_limits": {
                "count": c.outside.count, "of": c.outside.opportunities,
                "expected": sig(c.outside.expected, 3),
                **({"source": src} if (src := detector_source(c, "outside_limits")) else {}),
                "rate_interval": sig_pair(c.outside.rate_interval) if c.outside.rate_interval else None,
            } if c.outside else None,
            "detectors": detectors,
            "settings": {
                "ewma": f"lambda {EWMA_LAMBDA}, L {EWMA_L}", "cusum": f"k {CUSUM_K}, h {CUSUM_H}",
                "arl0": {k: sig(v, 3) for k, v in c.arl0.items()},
                "arl_1sigma_shift": {k: sig(v, 3) for k, v in c.arl_1sigma.items()},
                "gaps": GAP_HANDLING,
            },
            "first_violations": first,
        }  # fmt: skip

    # panel payload -------------------------------------------------------------
    def panel(
        self, dataset_id: str, start_ms: int | None, end_ms: int | None, ref: dict | None = None
    ) -> dict:
        prep, base, diags, used = self.run(dataset_id, start_ms, end_ms, ref)
        series = []
        caveats = list(prep.caveats)
        for sid, d in diags.items():
            labels, ts, y = prep.series[sid]
            c = d.chart
            item = {
                "id": sid,
                "labels": labels,
                "ts": [int(t) for t in ts],
                "value": [sig(v, 6) for v in y],
                "verdict": d.verdict,
                "also": d.also,
                "n": d.n,
            }
            if c is None or c.mode == "insufficient_data":
                item |= {"mode": "insufficient_data", "reason": c.reason if c else d.reasons[0]}
            else:
                vsrc = violation_sources(c, d.shifted_from)
                item |= {
                    "mode": c.mode,
                    "centre": [sig(v, 6) for v in c.centre],
                    "sigma": sig(c.sigma, 6),
                    "n_baseline": c.n_baseline,
                    "n_eff_baseline": sig(c.n_eff_baseline, 3),
                    # limit uncertainty (99%, from the baseline's n_eff): the level's interval
                    # and sigma's; the drawn limits inherit both
                    "level": sig(c.level, 6),
                    "centre_interval": sig_pair(c.centre_interval),
                    "sigma_interval": sig_pair(c.sigma_interval),
                    "seasonal": c.seasonal,
                    **({"seasonal_profile": used[sid]} if sid in used else {}),
                    "in_control": c.in_control,
                    # spec §5.4: each signal's source (special cause, common-cause false
                    # alarm, or undetermined)
                    "violations": [
                        {
                            "ts": int(ts[i]),
                            "value": sig(float(y[i]), 6),
                            "rules": rules,
                            "source": vsrc[i],
                        }
                        for i, rules in c.violations().items()
                    ],
                }
            add_caveats(caveats, d.caveats)
            series.append(item)
        return {
            "kind": "spc",
            "effective_step_ms": prep.step_ms,
            "baseline": {
                "start_ms": base[0],
                "end_ms": base[1],
                "basis": base[2],
                "kind": "reference" if ref else "window",
            },
            "series": series,
            "skipped": prep.skipped,
            "caveats": caveats,
        }
