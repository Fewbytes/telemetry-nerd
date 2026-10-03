"""binding_verdict (bead czt.4): per-signal verdicts for a USE / RED / Little's law binding.

Roles are resolved and planned as show_binding does (czt.3) and fetched through the same path
(error ratio = errors / requests; latency = the histogram), for now and for k reference windows
of the same length. The statistics live in analysis/verdicts.py; this module turns datasets into
per-step arrays, states the reference and the error budget, wires evidence statistics and, for
a panel group, annotates its roles. Design: docs/superpowers/specs/2026-10-02-binding-verdicts-design.md.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import polars as pl

from telemetry_nerd.analysis import sources
from telemetry_nerd.analysis.exprkind import rate_interval_ms
from telemetry_nerd.analysis.seasonal import DEFAULT_K, cycle_shifts
from telemetry_nerd.analysis.seasonal_dist import Hist, common_edges, distance, expit, pool
from telemetry_nerd.analysis.sources import COMMON, SPECIAL, UNDETERMINED
from telemetry_nerd.analysis.verdicts import (
    SLOW_SHARE,
    Judgement,
    Level,
    Onset,
    RoleInput,
    RoleResult,
    judge_roles,
    share_threshold,
)
from telemetry_nerd.catalog.relations import BINDING_ROLES
from telemetry_nerd.core.binding_ops import ROLE_FAILURES, BindingOps, parse_range
from telemetry_nerd.core.binding_view import RolePlan, natural_bound
from telemetry_nerd.core.events import Actor
from telemetry_nerd.core.littles_ops import NOT_POSSIBLE
from telemetry_nerd.core.uncertainty import mark_statistics
from telemetry_nerd.core.wire import add_caveats, measurement_caveats, sig, sig_pair, statistic
from telemetry_nerd.model.time import TimeRange, format_duration, iso
from telemetry_nerd.workspace.models import PanelGroup

if TYPE_CHECKING:
    from telemetry_nerd.core.service import TelemetryService

REFERENCES = ("auto", "previous", "day", "week", "profile")
SCHEMES = {"previous": "previous", "day": "1d", "week": "1w"}
SCHEME_LABEL = {"previous": "previous windows", "1d": "same window, previous days",
                "1w": "same window, previous weeks"}  # fmt: skip
#: forms judged per member (not additive across members); the rest on the total
PER_MEMBER = ("utilization", "saturation", "mean", "value")
MEMBERS_MAX = 20
_ABSENT_ERRORS_NOTE = (
    "no error series in the data: counted as 0 errors (error counters usually appear only after "
    "the first error)"
)

METHOD = {
    "share": (
        "share of requests per step against {k} reference windows: logit of the window share on "
        "effective n = n / (phi tau) (Pearson dispersion of per-step counts, autocorrelation of "
        "their residuals), Student-t prediction interval from the reference shares (k-1 df, "
        "never below sampling noise); onsets: two-sided CUSUM of Pearson residuals around now's "
        "own level, h from the in-control ARL for the window"
    ),
    "count": (
        "events per step against {k} reference windows: log rate, quasi-Poisson (dispersion x "
        "autocorrelation), Student-t prediction interval from the reference rates; onsets: "
        "CUSUM of Pearson residuals"
    ),
    "value": (
        "per-step value against {k} reference windows: window mean ({scale} scale) against a "
        "Student-t prediction interval from the reference means (within-window sigma and "
        "autocorrelation as the floor); onsets: two-sided CUSUM of residuals from now's own "
        "level (day/week shape removed), AR(1)-prewhitened when the reference is autocorrelated"
    ),
}


@dataclass
class _Role:
    plan: RolePlan
    now: str | None = None  # dataset id
    refs: list[str | None] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    threshold: dict | None = None
    inputs: list[str] = field(default_factory=list)  # datasets the statistics are computed from


def _labels_key(labels: str) -> str:
    return json.dumps(json.loads(labels), sort_keys=True)


class VerdictOps:
    def __init__(self, svc: TelemetryService) -> None:
        self.svc = svc

    # reference ---------------------------------------------------------------------------------
    def _scheme(self, reference: str, source: str, plans: dict[str, RolePlan]) -> tuple[str, str]:
        if reference in SCHEMES:
            return SCHEMES[reference], f"reference={reference} (asked)"
        periods: set[str] = set()
        for p in plans.values():
            for e in (p.expr, p.den, p.num):
                if e:
                    periods |= set(self.svc._profile_periods(source, e))
        if "hour_of_week" in periods:
            return "1w", "the cached operating profile is weekly (hour_of_week)"
        if "hour_of_day" in periods:
            return "1d", "the cached operating profile is daily (hour_of_day)"
        if reference == "profile":
            raise ValueError(
                "reference='profile' needs a seasonal operating profile of the roles' expressions "
                "and none is cached (hint: operating_profile(<a role's expr>) first, or "
                "reference='previous' / 'day' / 'week')"
            )
        return "previous", "no seasonal operating profile cached: the preceding windows"

    # data ----------------------------------------------------------------------------------------
    def _label_sets(self, ds: str) -> list[dict]:
        _, r = self.svc.datasets.get(ds)
        return [json.loads(row["labels"]) for row in r.series.to_pylist()]

    @staticmethod
    def _member(labels: dict, keys: frozenset[str] | None) -> str:
        """The member identity: the labels, restricted to `keys` when given."""
        return json.dumps(
            {k: v for k, v in labels.items() if keys is None or k in keys}, sort_keys=True
        )

    def _values(
        self, ds: str, shift: int, keys: frozenset[str] | None = None
    ) -> dict[str, dict[int, float]]:
        """Per member (labels key, restricted to `keys`): {ts on now's grid: avg}; series of one
        member that differ only in the dropped labels are summed."""
        _, r = self.svc.datasets.get(ds)
        labels = {
            row["series_id"]: self._member(json.loads(row["labels"]), keys)
            for row in r.series.to_pylist()
        }
        out: dict[str, dict[int, float]] = {}
        for row in r.buckets.to_pylist():
            v = row["avg"]
            if v is None or not math.isfinite(v):
                continue
            pts = out.setdefault(labels.get(row["series_id"], "{}"), {})
            t = row["ts_ms"] + shift
            pts[t] = pts.get(t, 0.0) + v
        return out

    def _member_keys(self, ds: str, keys: frozenset[str] | None = None) -> set[str]:
        """The members a dataset has a series for, whether or not any step has a value."""
        return {self._member(lb, keys) for lb in self._label_sets(ds)}

    @staticmethod
    def _arr(points: dict[int, float], grid: np.ndarray) -> np.ndarray:
        return np.array([points.get(int(t), np.nan) for t in grid])

    def _total(self, ds: str | None, grid: np.ndarray, shift: int) -> np.ndarray:
        if ds is None:
            return np.full(grid.size, np.nan)
        out = np.full(grid.size, np.nan)
        for pts in self._values(ds, shift).values():
            a = self._arr(pts, grid)
            out = np.where(np.isnan(out), a, np.where(np.isnan(a), out, out + a))
        return out

    def _ratio_counts(
        self, ds: str | None, grid: np.ndarray, shift: int, step_s: float, stats: dict
    ) -> tuple[np.ndarray, np.ndarray]:
        """errors and requests per step (rate x step, summed over members) behind a ratio.

        Only an error series that is entirely absent reads as 0 errors (counters usually appear
        after the first error), and `notes` says so (principles 4, 11). A step the error series
        does not cover (unknown, untrusted, NaN) stays NaN: judged on neither side, never 0."""
        if ds is None:
            nan = np.full(grid.size, np.nan)
            return nan, nan
        num, den = self.svc.datasets.meta(ds).parents[:2]
        side = "now" if shift == 0 else "ref"
        keys: frozenset[str] | None = None
        if num is not None and self._label_sets(num):
            # pair on the label names both series carry (errors may carry an extra `status`)
            num_names = set().union(*(lb.keys() for lb in self._label_sets(num)))
            den_names = set().union(*(lb.keys() for lb in self._label_sets(den)))
            common = frozenset(num_names & den_names)
            if num_names and den_names and not common:
                stats["unpaired"] = True  # no shared identity: not paired, nothing counted as 0
                nan = np.full(grid.size, np.nan)
                return nan, nan
            keys = common
        dv = self._values(den, shift, keys)
        nv = self._values(num, shift, keys) if num is not None else {}
        nkeys = self._member_keys(num, keys) if num is not None else set()
        a = np.full(grid.size, np.nan)
        n = np.full(grid.size, np.nan)
        for mem, dpts in dv.items():
            d = self._arr(dpts, grid)
            if not nkeys:
                e = np.zeros(grid.size)  # no error series at all: 0 errors, disclosed
            elif mem not in nkeys:
                stats["zero_" + side].add(mem)  # this member has none: 0, disclosed
                e = np.zeros(grid.size)
            else:
                e = self._arr(nv.get(mem, {}), grid)
            ok = ~np.isnan(d)
            stats["gaps_" + side] += int((ok & np.isnan(e)).sum())  # lost data, out of both sides
            ok &= ~np.isnan(e)
            a = np.where(ok, np.where(np.isnan(a), 0.0, a) + e, a)
            n = np.where(ok, np.where(np.isnan(n), 0.0, n) + d, n)
        a, n = a * step_s, n * step_s
        if not nkeys:
            stats["all_absent_" + side] = True
        a = np.clip(a, 0, None)
        return np.minimum(a, n), n

    @staticmethod
    def _ratio_stats() -> dict:
        return {"zero_now": set(), "zero_ref": set(), "gaps_now": 0, "gaps_ref": 0}

    @staticmethod
    def _ratio_notes(stats: dict, notes: list[str]) -> None:
        if stats.get("unpaired"):
            notes.append("error and request series do not share member labels; not paired")
        if stats.get("all_absent_now"):
            notes.append(_ABSENT_ERRORS_NOTE)
        elif stats.get("all_absent_ref"):
            notes.append(_ABSENT_ERRORS_NOTE + " (in a reference window)")
        for side, label in (("now", ""), ("ref", "reference windows: ")):
            zero = sorted(stats["zero_" + side])
            parts = []
            if zero:
                shown = ", ".join(zero[:3]) + (f" (+{len(zero) - 3})" if len(zero) > 3 else "")
                parts.append(f"members without an error series counted as 0 errors: {shown}")
            if stats["gaps_" + side]:
                parts.append(
                    f"error series has no value at {stats['gaps_' + side]} member-steps: "
                    "excluded, not counted as 0"
                )
            if parts:
                notes.append(label + "; ".join(parts))

    def _hist(self, ds: str, j: int, shift: int) -> tuple[Hist, pl.DataFrame, pl.DataFrame]:
        meta, dist = self.svc.datasets.get_distribution(ds)
        rows = pl.from_arrow(dist.rows)
        cols = pl.from_arrow(dist.columns)
        assert isinstance(rows, pl.DataFrame) and isinstance(cols, pl.DataFrame)
        rows = rows.with_columns(pl.col("ts_ms") + shift)
        cols = cols.with_columns(pl.col("ts_ms") + shift)
        w = (
            rows.group_by("bucket_lo", "bucket_hi")
            .agg(pl.col("count").sum())
            .sort("bucket_hi", "bucket_lo")
        )
        h = Hist(
            j, meta.start_ms - meta.step_ms, meta.end_ms, w["bucket_lo"].to_numpy().astype(float),
            w["bucket_hi"].to_numpy().astype(float), w["count"].to_numpy().astype(float),
            dataset=ds,
        )  # fmt: skip
        return h, rows, cols

    @staticmethod
    def _over(rows: pl.DataFrame, cols: pl.DataFrame, x: float, grid: np.ndarray):
        n = dict(cols.group_by("ts_ms").agg(pl.col("n").sum()).iter_rows())
        a = dict(
            rows.filter(pl.col("bucket_lo") >= x)
            .group_by("ts_ms")
            .agg(pl.col("count").sum())
            .iter_rows()
        )
        nn = np.array([n.get(int(t), np.nan) for t in grid], float)
        aa = np.array([a.get(int(t), 0.0) for t in grid], float)
        return np.where(np.isnan(nn), np.nan, aa), nn

    # the call ------------------------------------------------------------------------------------
    async def verdict(
        self,
        source: str = "default",
        kind: str | None = None,
        key: str | None = None,
        group: str | None = None,
        suggestion: str | None = None,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        reference: str = "auto",
        tz: str = "UTC",
        matchers: dict[str, str] | None = None,
        error_matcher: str | None = None,
        alpha: float = 0.05,
        actor: Actor = "claude",
    ) -> dict:
        svc = self.svc
        if reference not in REFERENCES:
            raise ValueError(f"reference is one of {', '.join(REFERENCES)}")
        if not 0 < alpha <= 0.2:
            raise ValueError("alpha is the family-wise error rate: 0 < alpha <= 0.2")
        g: PanelGroup | None = None
        if group:
            g = svc.ws.group_get(group)
            source, kind, key = g.source, g.kind, g.key
            suggestion = g.suggestion if g.basis == "suggestion" else None
            matchers, error_matcher = dict(g.matchers), g.error_matcher
        b = svc.bindings.resolve(source, kind, key, suggestion)
        mt = dict(matchers or {})
        infos, plans = svc.bindings.plans(source, b, mt, error_matcher)
        src = svc._source(source)
        res_ms = src.resolution_ms
        if g is not None:
            rng, step_ms = TimeRange(g.start_ms, g.end_ms), g.step_ms
        else:
            rng = parse_range(start, end, svc.clock())
            step_ms = BindingOps.grid_step(rng, step, res_ms, plans.values())
        scheme, basis = self._scheme(reference, source, plans)
        k = DEFAULT_K[scheme]
        when = {
            "start": str(rng.start_ms),
            "end": str(rng.end_ms),
            "step": format_duration(step_ms),
        }
        panels = {r.role: r.panel for r in g.roles if r.panel} if g else {}
        roles = {r: _Role(p) for r, p in plans.items()}
        await self._fetch_now(source, roles, panels, when, actor)
        first = next((st.now for st in roles.values() if st.now), None)
        if first is None:
            raise ValueError(
                "no role could be fetched: "
                + "; ".join(f"{r}: {st.error}" for r, st in roles.items())
            )
        m0 = svc.datasets.meta(first)
        grid = np.arange(m0.start_ms, m0.end_ms + 1, m0.step_ms, dtype=np.int64)
        span = grid.size * m0.step_ms
        shifts = cycle_shifts(int(grid[0]), scheme, k, tz, span, m0.step_ms)
        windows = [[iso(int(grid[0]) - m0.step_ms - s), iso(int(grid[-1]) - s)] for s in shifts]
        live = [r for r, st in roles.items() if st.now]
        await self._fetch_refs(source, [roles[r] for r in live], grid, m0.step_ms, shifts, k, actor)

        step_s = m0.step_ms / 1000
        inputs: dict[str, RoleInput] = {}
        for r in live:
            st = roles[r]
            try:
                inputs[r] = self._input(st, grid, shifts, step_s, m0.step_ms, res_ms, scheme)
            except ROLE_FAILURES as e:
                st.error = str(e)
        results, fam, order = judge_roles(inputs, grid, m0.step_ms, alpha)

        model = None
        if b.kind == "littles_law" and set(BINDING_ROLES["littles_law"]) <= infos.keys():
            model = await self._littles(source, b, infos, mt, rng, actor)

        out_roles: dict[str, dict] = {}
        caveats: list[str] = []
        for role in [*plans, *(r for r in b.roles if r not in plans)]:
            if role not in plans:
                u = b.unfilled.get(role) or {}
                out_roles[role] = {"status": "gap", "suggest": u.get("name"), "why": u.get("why")}
                if b.kind == "littles_law" and role == "concurrency":
                    out_roles[role]["model_check"] = {
                        "status": "not_possible",
                        "summary": NOT_POSSIBLE + ": the binding has no concurrency signal; L "
                        "was not estimated (instrument the suggested in-flight gauge)",
                    }
            elif role not in results:
                out_roles[role] = _error_wire(roles[role])
            else:
                d = self._wire(role, roles[role], results[role], k, grid, m0.step_ms)
                if role == "concurrency" and model is not None:
                    d["model_check"] = model
                add_caveats(caveats, d.get("caveats", []))
                out_roles[role] = d
        summary = self._summary(out_roles, results, order)
        variation = [
            {**v, "role": role}
            for role, d in out_roles.items()
            for v in [*d.get("variation", []), *(d.get("model_check") or {}).get("variation", [])]
        ]
        out: dict[str, Any] = {
            "binding": {"kind": b.kind, "key": b.key, "basis": b.basis,
                        **({"suggestion": b.suggestion} if b.suggestion else {})},
            **({"group": g.id} if g else {}),
            "question": "Did each golden signal move against its reference, how, and which first?",
            "range": [iso(int(grid[0]) - m0.step_ms), iso(int(grid[-1]))],
            "step": format_duration(m0.step_ms),
            "reference": {
                "scheme": scheme, "label": SCHEME_LABEL[scheme], "cycles": k, "tz": tz,
                "windows": windows, "chosen": basis,
            },
            "family": {
                "alpha": alpha, "roles_judged": fam.roles, "per_role": sig(fam.per_role),
                "per_detector": sig(fam.per_detector),
                "method": (
                    f"Bonferroni: family-wise false-alarm rate <= {alpha:g} over {fam.roles} "
                    f"judged role(s); each role {fam.per_role:.3g}, split between its level and "
                    "episode detectors; per-member roles split it again across members. "
                    "near_bound and model_check are not tests in this family."
                ),
            },
            "roles": out_roles,
            "summary": summary,
            # spec §5.4: every role's labelled findings (and the Little's law check's) in one list
            "variation": variation,
            "caveats": caveats,
        }  # fmt: skip
        if g is not None:
            self._annotate(g, out_roles, summary, out["reference"], alpha, actor)
        return out

    async def _fetch_now(
        self, source: str, roles: dict[str, _Role], panels: dict[str, str], when: dict, actor: Actor
    ) -> None:
        """Each role's dataset for now: its group panel's, else fetched as show_binding does."""
        svc = self.svc

        async def now_ds(role: str, p: RolePlan) -> str:
            if pid := panels.get(role):
                return svc.workspace.get_panel(pid).dataset_ids[0]
            return (await svc.bindings.fetch(source, p, when, actor))["dataset"]

        got = await asyncio.gather(
            *(now_ds(r, st.plan) for r, st in roles.items()), return_exceptions=True
        )
        for st, x in zip(roles.values(), got, strict=True):
            if isinstance(x, BaseException):
                if not isinstance(x, ROLE_FAILURES):
                    raise x
                st.error = str(x)
            else:
                st.now = x

    async def _fetch_refs(
        self,
        source: str,
        live: list[_Role],
        grid: np.ndarray,
        step_ms: int,
        shifts: list[int],
        k: int,
        actor: Actor,
    ) -> None:
        """The k reference windows of each fetched role (None where one could not be fetched)."""

        async def ref_ds(p: RolePlan, shift: int) -> str | None:
            w = {
                "start": str(int(grid[0]) - shift),
                "end": str(int(grid[-1]) - shift),
                "step": format_duration(step_ms),
            }
            try:
                return (await self.svc.bindings.fetch(source, p, w, actor))["dataset"]
            except ROLE_FAILURES:
                return None

        fetched = await asyncio.gather(*(ref_ds(st.plan, s) for st in live for s in shifts))
        for i, st in enumerate(live):
            st.refs = list(fetched[i * k : (i + 1) * k])
            if missing := sum(d is None for d in st.refs):
                st.notes.append(f"{missing} of {k} reference windows could not be fetched")

    def _input(
        self,
        st: _Role,
        grid: np.ndarray,
        shifts: list[int],
        step_s: float,
        step_ms: int,
        res_ms: int,
        scheme: str,
    ) -> RoleInput:
        p = st.plan
        texts = [x for x in (p.expr, p.num, p.den) if x]
        uses_rate = p.form == "distribution" or any("rate(" in t for t in texts)
        look = rate_interval_ms(step_ms, res_ms) if uses_rate else 0
        phase = scheme in ("1d", "1w")
        refs = list(zip(st.refs, shifts, strict=True))
        ok_refs = [(d, s) for d, s in refs if d is not None]
        assert st.now is not None
        if p.form == "error_ratio":
            meta = self.svc.datasets.meta(st.now)
            st.inputs = [*meta.parents]
            for d, _ in ok_refs:
                st.inputs += self.svc.datasets.meta(d).parents
            stats = self._ratio_stats()
            now_c = self._ratio_counts(st.now, grid, 0, step_s, stats)
            ref_c = [self._ratio_counts(d, grid, s, step_s, stats) for d, s in ok_refs]
            self._ratio_notes(stats, st.notes)
            return RoleInput("share", now_c, ref_c, lookback_ms=look)
        st.inputs = [st.now, *(d for d, _ in ok_refs)]
        if p.form == "distribution":
            h0, r0, c0 = self._hist(st.now, 0, 0)
            hs = [(self._hist(d, j, s), d) for j, (d, s) in enumerate(ok_refs, 1)]
            edges = common_edges([h0, *(h for (h, _, _), _ in hs)])
            x, enough = share_threshold([h for (h, _, _), _ in hs], edges)
            if x is None:
                raise ValueError(
                    "no bucket edge shared by now and the reference windows splits the "
                    "requests (no latency threshold to judge)"
                )
            pooled = [h for (h, _, _), _ in hs]
            n_ref = sum(h.n for h in pooled)
            st.threshold = {
                "x": x, "target_share": SLOW_SHARE,
                "reference_share": sig(sum(h.over(x) for h in pooled) / n_ref) if n_ref else None,
                "now_share": sig(h0.over(x) / h0.n) if h0.n else None,
                "shape_distance": sig(_shape(h0, pooled, edges)),
                "basis": "the bucket edge nearest the reference windows' ~p95 (exact at the edge)",
            }  # fmt: skip
            if not enough:
                st.notes.append("few_over_threshold: < 20 reference requests above per window")
            return RoleInput(
                "share", self._over(r0, c0, x, grid),
                [self._over(rr, cc, x, grid) for (_, rr, cc), _ in hs], lookback_ms=look,
            )  # fmt: skip
        count = p.form == "errors" and any("rate(" in t for t in texts)
        if count:
            exposure = lambda a: np.where(np.isnan(a), np.nan, step_s)
            now = self._total(st.now, grid, 0) * step_s
            refs_a = [self._total(d, grid, s) * step_s for d, s in ok_refs]
            return RoleInput(
                "count", (now, exposure(now)), [(a, exposure(a)) for a in refs_a],
                lookback_ms=look,
            )  # fmt: skip
        if p.form in PER_MEMBER:
            mem_now = self._values(st.now, 0)
            mem_refs = [self._values(d, s) for d, s in ok_refs]
            keys = sorted(mem_now, key=lambda m: -len(mem_now[m]))[:MEMBERS_MAX]
            if len(mem_now) > MEMBERS_MAX:
                st.notes.append(f"{len(mem_now)} members: the {MEMBERS_MAX} with most data judged")
            members = {
                m: (self._arr(mem_now[m], grid), [self._arr(mr.get(m, {}), grid) for mr in mem_refs])
                for m in keys
            }  # fmt: skip
            bound = _utilization_bound(p, members) if p.form == "utilization" else None
            if "throttl" in p.metric:
                st.notes.append("saturation as throttling: time or periods throttled")
            if len(members) == 1:
                ((now, rs),) = members.values()
                return RoleInput(
                    "value", now, rs, lookback_ms=look, phase_aligned=phase, bound=bound
                )
            return RoleInput(
                "value", members=members, lookback_ms=look, phase_aligned=phase, bound=bound
            )
        return RoleInput(
            "value", self._total(st.now, grid, 0), [self._total(d, grid, s) for d, s in ok_refs],
            lookback_ms=look, phase_aligned=phase,
        )  # fmt: skip

    async def _littles(self, source, b, infos, mt, rng: TimeRange, actor: Actor) -> dict:
        try:
            out = await self.svc.bindings.check_littles(source, b, infos, mt, rng, actor)
        except ROLE_FAILURES as e:
            return {"error": str(e)}
        t = out["total"]
        return {
            "summary": out["summary"], "discrepancy": out["discrepancy"],
            "verdict": out["verdict"], "classification": out["classification"],
            "warnings": out["warnings"], "variation": out["variation"],
            "ratio": t.get("ratio"), "ci95": t.get("ci95"),
            "L": t.get("L"), "lambda_W": t.get("lambda_W"), "W_s": t.get("W_s"),
            "window": out["window"], "dataset": out["datasets"]["concurrency"],
            "evidence": t.get("evidence", []),
            "note": "check_littles_law (czt.2): the discrepancy L - λW with its measurement "
            "interval; systematic offset = measurement system, transient windows = special or "
            "common cause; its own 5% FWER, not a test in this family",
        }  # fmt: skip

    # wire ----------------------------------------------------------------------------------------
    def _wire(
        self, role: str, st: _Role, res: RoleResult, k: int, grid: np.ndarray, step: int
    ) -> dict:
        j = res.judgement
        p = st.plan
        ds = st.now or ""
        d: dict[str, Any] = {
            "status": j.status, "metric": p.metric, "form": p.form, "dataset": st.now,
            "method": METHOD[j.kind].format(k=k, scale=j.level.scale if j.level else "log"),
        }  # fmt: skip
        if j.direction:
            d["direction"] = j.direction
            d["pattern"] = j.pattern
        if res.member is not None:
            d["member"] = json.loads(res.member)
            d["members"] = {
                "judged": len(res.members),
                "changed": [json.loads(m) for m, mj in res.members if mj.status == "changed"],
            }
        d["alpha"] = {"level": sig(res.alpha_level), "episode": sig(res.alpha_episode)}
        if st.threshold:
            d["threshold"] = st.threshold
        params: dict[str, Any] = {"role": role, "form": p.form}
        if p.form == "error_ratio":
            params["num_den"] = self.svc.datasets.meta(ds).parents[:2] if ds else []
        if st.threshold:
            params["threshold"] = st.threshold["x"]
        evidence: list[dict] = []
        lv = j.level
        if lv is not None:
            name, val, iv, d["level"] = _level_wire(j, lv)
            evidence.append(
                statistic(ds, f"{role}_{name}", sig(val), sig_pair(iv),
                          d["method"], {**params, "p": sig(lv.p), "scale": lv.scale})
            )  # fmt: skip
            evidence[-1]["_varies"] = True
        if j.now_value is not None and j.kind != "value":
            evidence.append(
                statistic(ds, f"{role}_{'share' if j.kind == 'share' else 'rate'}_now",
                          sig(j.now_value), sig_pair(j.now_interval) if j.now_interval else None,
                          "Wilson 95% on effective n" if j.kind == "share"
                          else "quasi-Poisson 95% on effective counts",
                          {**params, "n_eff": sig(j.n_eff_now)})
            )  # fmt: skip
        if j.episodes:
            d["episodes"] = _episodes_wire(j, grid, step)
            d["episode_threshold_h"] = sig(j.stage.h, 3) if j.stage else None
        if j.onset is not None:
            d["onset"] = _onset_wire(j.onset)
            if j.onset.at_ms is not None and j.onset.lo_ms is not None:
                evidence.append(
                    statistic(ds, f"{role}_onset_ms", j.onset.at_ms,
                              [j.onset.lo_ms, j.onset.hi_ms],
                              f"onset ({j.onset.basis}): Page's change time of the CUSUM "
                              "excursion, or the single changepoint with Bai's 95% interval; "
                              "interval reaches back one block and the rate window",
                              params)
                )  # fmt: skip
                evidence[-1]["_varies"] = True
        near = {m: nb for m, nb in res.near.items() if nb.runs}
        if res.near:
            any_nb = next(iter(res.near.values()))
            d["near_bound"] = {
                "bound": any_nb.bound, "threshold": sig(any_nb.threshold),
                "runs": {
                    _member_text(m): [[iso(int(grid[a]) - step), iso(int(grid[b]))] for a, b in nb.runs]
                    for m, nb in near.items()
                },
                "rule": f"runs of >= 3 steps at >= {int(100 * 0.9)}% of the natural bound "
                "(exact counts; a stated rule, not a test)",
            }  # fmt: skip
            if near and j.status == "changed":
                d["at_capacity"] = True
        if j.reasons:
            d["reasons"] = j.reasons
        cav = list(dict.fromkeys(j.caveats))
        if cav:
            d["caveats"] = cav
        if st.notes:
            d["notes"] = st.notes
        d["evidence"] = evidence
        d = mark_statistics(d, self.svc.datasets, st.inputs)
        # spec §5.4: a change beyond the reference cycles is a special cause, unless the data
        # it rests on has measurement-system issues (then the two cannot be told apart); no
        # change is the common-cause envelope
        meas = sources.measurement_caveats([
            *d.get("caveats", []),
            *(c for did in st.inputs for c in measurement_caveats(self.svc.datasets.meta(did))),
        ])  # fmt: skip
        src = {"changed": UNDETERMINED if meas else SPECIAL, "no_change": COMMON}.get(j.status)
        for e in evidence:
            if e.pop("_varies", False) and src:
                e["source"] = src
        if src:
            d["source"] = src
        d["text"] = _role_text(role, d)
        d["variation"] = (
            [sources.item(src, d["text"] + (
                ": the data has measurement-system issues, so special cause and measurement "
                "cannot be told apart" if src == UNDETERMINED else ""
            ))] if src else []
        ) + sources.measurement_items(meas)  # fmt: skip
        return d

    def _summary(self, roles: dict[str, dict], results: dict[str, RoleResult], order) -> dict:
        moved = [r for r in order.order]
        still = [r for r, d in roles.items() if d.get("status") == "no_change"]
        if not moved:
            text = "No golden signal moved against the reference" + (
                f" ({', '.join(still)} unchanged)." if still else "."
            )
            return {"moved": [], "first": None, "order": [], "text": text}
        parts = []
        for i, cluster in enumerate(order.clusters):
            if len(cluster) == 1:
                r = cluster[0]
                parts.append(f"{'first ' if i == 0 else 'then '}{roles[r]['text']}")
            else:
                ons = [results[r].judgement.onset for r in cluster]
                los = [o.lo_ms for o in ons if o and o.lo_ms is not None]
                his = [o.hi_ms for o in ons if o]
                pm = (
                    f"±{format_duration(int((max(his) - min(los)) / 2))}"
                    if los and len(los) == len(ons)
                    else "within their intervals"
                )
                parts.append(
                    f"{'first ' if i == 0 else 'then '}{' and '.join(cluster)} together "
                    f"(simultaneous {pm}: onset intervals overlap): "
                    + "; ".join(roles[r]["text"] for r in cluster)
                )
        text = "; ".join(parts) + (f". Unchanged: {', '.join(still)}." if still else ".")
        return {
            "moved": moved,
            "first": order.first,
            "simultaneous": [c for c in order.clusters if len(c) > 1],
            "order": [
                {"role": r, **_onset_wire(results[r].judgement.onset)}
                for r in moved
                if results[r].judgement.onset
            ],
            "text": text[0].upper() + text[1:],
        }

    def _annotate(
        self, g: PanelGroup, roles: dict, summary: dict, ref: dict, alpha: float, actor: Actor
    ) -> None:
        new = []
        for r in g.roles:
            d = roles.get(r.role)
            if d is None or d.get("status") == "gap":
                new.append(r)
                continue
            v = {
                "status": d["status"], "direction": d.get("direction"),
                "pattern": d.get("pattern"), "text": d.get("text") or d.get("error"),
                "at_capacity": bool(d.get("at_capacity")),
                **({"source": d["source"]} if d.get("source") else {}),
                **({"onset": d["onset"]} if d.get("onset") else {}),
            }  # fmt: skip
            new.append(r.model_copy(update={"verdict": v}))
        gv = {
            "text": summary["text"], "first": summary.get("first"), "moved": summary["moved"],
            "reference": ref["label"], "alpha": alpha, "at_ms": self.svc.clock(),
        }  # fmt: skip
        self.svc.ws.objects.set_group(g.model_copy(update={"roles": new, "verdict": gv}))
        self.svc.log.append(actor, "panel_group.updated", g.id, {"verdict": summary["moved"]})


# helpers ---------------------------------------------------------------------------------------
def _utilization_bound(p: RolePlan, members: dict) -> float | None:
    vals = np.concatenate([np.r_[now, *refs] for now, refs in members.values()])
    vals = vals[~np.isnan(vals)]
    if vals.size == 0:
        return None
    return natural_bound(vals.min(), vals.max(), p.expr or "")


def _shape(now: Hist, refs: list[Hist], edges: np.ndarray) -> float:
    return distance(now, pool(refs), edges) if refs else math.nan


def _level_wire(j: Judgement, lv: Level) -> tuple[str, float, list[float], dict]:
    """(statistic name, effect, its 95% interval, the wire `level`) of a level test, on the
    data's scale: an odds ratio (logit), a ratio (log) or a difference (linear)."""
    lo, hi = lv.effect_interval()
    n90 = lv.normal()
    if lv.scale == "linear":
        name, val, iv = "level_difference_vs_reference", lv.effect, [lo, hi]
        normal = [n90[0], n90[1]]
        ref_value = lv.centre
    else:
        name = "odds_ratio_vs_reference" if lv.scale == "logit" else "ratio_vs_reference"
        val, iv = math.exp(lv.effect), [math.exp(lo), math.exp(hi)]
        conv = expit if lv.scale == "logit" else math.exp
        normal = [conv(n90[0]), conv(n90[1])]
        ref_value = conv(lv.centre)
    level = {
        "now": sig(j.now_value), "now_ci95": sig_pair(j.now_interval) if j.now_interval else None,
        "reference": sig(ref_value), "normal_90": sig_pair(normal),
        name.removesuffix("_vs_reference"): sig(val), "ci95": sig_pair(iv),
        "p": sig(lv.p), "flagged": lv.flagged, "reference_windows": len(lv.previous),
        **({"excluded_atypical": lv.excluded} if lv.excluded else {}),
    }  # fmt: skip
    return name, val, iv, level


def _episodes_wire(j: Judgement, grid: np.ndarray, step: int) -> list[dict]:
    """The first five CUSUM episodes, block indexes as times on now's grid."""

    def start(i: int) -> str:
        return iso(int(grid[min(i * j.block, grid.size - 1)]) - step)

    def end(i: int) -> str:
        return iso(int(grid[min((i + 1) * j.block, grid.size) - 1]))

    return [
        {
            "direction": "higher" if e.side > 0 else "lower",
            "start": start(e.start),
            "detected": end(e.signal),
            "ended": None if e.end is None else end(e.end),
            "peak_sigma": sig(e.peak, 3),
        }
        for e in j.episodes[:5]
    ]


def _error_wire(st: _Role) -> dict:
    return {
        "status": "error", "metric": st.plan.metric, "form": st.plan.form, "error": st.error,
        **({"notes": st.notes} if st.notes else {}),
    }  # fmt: skip


def _member_text(m: str) -> str:
    return ", ".join(f"{k}={v}" for k, v in json.loads(m).items()) if m else "total"


def _onset_wire(o: Onset | None) -> dict:
    if o is None:
        return {}
    if o.at_ms is None:
        return {"at": None, "before": iso(o.hi_ms), "basis": o.basis}
    return {
        "at": iso(o.at_ms),
        "interval": [iso(o.lo_ms) if o.lo_ms is not None else None, iso(o.hi_ms)],
        "basis": o.basis,
    }


def _hm(s: str | None) -> str:
    return "?" if s is None else s[11:16] + "Z"


def _role_text(role: str, d: dict) -> str:
    st = d["status"]
    if st == "no_change":
        return f"{role}: no change" + (" (common cause)" if d.get("source") else "")
    if st != "changed":
        return f"{role}: {st}" + (f" ({'; '.join(d['reasons'])})" if d.get("reasons") else "")
    o = d.get("onset") or {}
    if o.get("at"):
        lo, hi = o["interval"]
        when = f"from {_hm(o['at'])} ({_hm(lo)}–{_hm(hi)})"
    else:
        when = "already at the window's start"
    lv = d.get("level") or {}
    eff = next(
        (f"{k.replace('_', ' ')} {lv[k]:g}" for k in ("odds_ratio", "ratio", "level_difference")
         if lv.get(k) is not None),
        "",
    )  # fmt: skip
    cap = ", at capacity" if d.get("at_capacity") else ""
    src = f" — {sources.text(d['source'])}" if d.get("source") else ""
    return (
        f"{role} {d['direction']} ({d['pattern']}{cap}) {when}" + (f", {eff}" if eff else "") + src
    )
