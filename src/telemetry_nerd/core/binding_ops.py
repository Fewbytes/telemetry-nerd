"""show_binding (bead czt.3): a bound USE / RED / Little's law metric set as one panel group.

Each role is fetched through the ordinary query / query_distribution paths and drawn with
`show`, in the form binding_view plans for it, over one shared time range and step. A role with
many members is drawn as a fleet; a role with no signal becomes a gap card naming the
instrumentation that would fill it. The group is a workspace object; its panels carry its id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from telemetry_nerd.catalog.binding_suggest import find_suggestion, role_candidates
from telemetry_nerd.catalog.relations import BINDING_ROLES, SUGGESTIONS
from telemetry_nerd.charts.spec import LINE_SERIES_BUDGET, GroupRef
from telemetry_nerd.charts.yview import value_stats
from telemetry_nerd.core.binding_view import (
    DIST_FACETS_MAX,
    RATIO_LEVEL,
    RATIO_METHOD,
    Hint,
    MetricInfo,
    RolePlan,
    error_ratio,
    littles_selectors,
    natural_bound,
    plan_role,
)
from telemetry_nerd.core.events import Actor
from telemetry_nerd.core.littles_ops import NOT_POSSIBLE
from telemetry_nerd.datasets.store import Lineage
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.time import TimeRange, format_duration, parse_duration, parse_time
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.workspace.models import GroupRole, MetricSuggestion, PanelGroup

if TYPE_CHECKING:
    from telemetry_nerd.core.service import TelemetryService

#: what fetching or drawing one role may fail with: the role shows the error, the rest still draw
ROLE_FAILURES = (SourceError, ValueError, NotFound, LookupError)


def parse_range(start: str, end: str, now_ms: int) -> TimeRange:
    rng = TimeRange(parse_time(start, now_ms), parse_time(end, now_ms))
    if rng.end_ms <= rng.start_ms:
        raise ValueError("the range must end after it starts")
    return rng


@dataclass(frozen=True)
class Resolved:
    kind: str
    key: str
    roles: dict[str, str | None]
    join_on: list[str]
    hints: dict[str, Hint]
    unfilled: dict[str, dict]  # role -> {name, type, labels, why, gap?}
    basis: str  # binding | suggestion
    origin: str | None = None
    suggestion: str | None = None
    native: frozenset[str] = frozenset()


def _gap_role(b: Resolved, role: str) -> GroupRole:
    """An unfilled role: the instrumentation that would fill it (and its gap, if one is open)."""
    u = b.unfilled.get(role) or {}
    h = SUGGESTIONS[(b.kind, role)]
    return GroupRole(
        role=role, view="gap",
        suggestion=MetricSuggestion(
            name=u.get("name") or h.metric_name(b.key),
            type=u.get("type") or h.type,
            labels=list(u.get("labels") or h.labels),
        ),
        why=u.get("why") or h.why, gap=u.get("gap"),
    )  # fmt: skip


class BindingOps:
    def __init__(self, svc: TelemetryService) -> None:
        self.svc = svc

    # resolution ---------------------------------------------------------------------------------
    def resolve(
        self, source: str, kind: str | None, key: str | None, suggestion: str | None
    ) -> Resolved:
        """The binding to draw or judge: a binding_suggest proposal (by id) or a confirmed binding
        (by kind and key)."""
        if suggestion:
            return self._from_suggestion(source, key, suggestion)
        if not kind or not key:
            raise ValueError(
                "show_binding needs kind and key of a confirmed binding, or a suggestion id "
                "(binding_suggest)"
            )
        return self._from_binding(source, kind, key)

    def _from_suggestion(self, source: str, key: str | None, suggestion: str) -> Resolved:
        s = find_suggestion(self.svc.ws.binding_suggest(source, key=key, limit=1000), suggestion)
        detail = s["detail"]
        return Resolved(
            kind=s["kind"], key=s["key"], roles=dict(s["roles"]), join_on=list(s["join_on"]),
            hints={r: Hint(d["form"], d.get("expr")) for r, d in detail.items()},
            unfilled={u["role"]: dict(u["suggest_instrumentation"]) for u in s["unfilled"]},
            basis="suggestion", suggestion=s["id"],
            native=frozenset(d["metric"] for d in detail.values() if d.get("histogram") == "native"),
        )  # fmt: skip

    def _from_binding(self, source: str, kind: str, key: str) -> Resolved:
        ws = self.svc.ws
        found = [
            b for b in ws.relations.bindings("catalog", source, kind=kind, key=key)
            if not b.winner.retracted
        ]  # fmt: skip
        if not found:
            have = [f"{b.kind}:{b.key}" for b in ws.relations.bindings("catalog", source)]
            raise NotFound(
                f"no confirmed {kind} binding for {key!r} on {source!r}"
                + (f"; bound: {', '.join(have)}" if have else "")
                + ". Use binding_suggest and pass its id as `suggestion`, or confirm one first."
            )
        w = found[0].winner
        hints, native = self._hints_for(source, kind, w.roles)
        gaps = ws.relations.binding_gaps("catalog", source, kind, key)
        unfilled = {}
        for role, metric in w.roles.items():
            if metric is None:
                h = SUGGESTIONS[(kind, role)]
                unfilled[role] = {
                    "name": h.metric_name(key), "type": h.type,
                    "labels": list(h.labels), "why": h.why, "gap": gaps.get(role),
                }  # fmt: skip
        return Resolved(
            kind=kind, key=key, roles=dict(w.roles), join_on=list(w.join_on), hints=hints,
            unfilled=unfilled, basis="binding", origin=w.origin, native=native,
        )  # fmt: skip

    def _hints_for(
        self, source: str, kind: str, roles: Mapping[str, str | None]
    ) -> tuple[dict[str, Hint], frozenset[str]]:
        """Forms and expr hints for a confirmed binding's metrics, from the suggestions that
        propose the same metrics (most roles in common first)."""
        try:
            sugg = self.svc.ws.binding_suggest(source, kind=kind, limit=1000)["suggestions"]
        except ValueError:
            return {}, frozenset()

        def overlap(s: dict) -> int:
            return sum(
                1
                for r, m in roles.items()
                if m and any(c["metric"] == m for c in role_candidates(s, r))
            )

        hints: dict[str, Hint] = {}
        native: set[str] = set()
        for s in sorted(sugg, key=overlap, reverse=True):
            for role, metric in roles.items():
                if metric is None or role in hints:
                    continue
                for c in role_candidates(s, role):
                    if c["metric"] == metric:
                        hints[role] = Hint(c["form"], c.get("expr"))
                        if c.get("histogram") == "native":
                            native.add(metric)
                        break
        return hints, frozenset(native)

    def _info(self, source: str, metric: str, native: frozenset[str]) -> MetricInfo:
        """Type and histogram kind of a role's metric. A classic histogram's base name is not a
        series, so a source's catalog often holds only its `_bucket`/`_count`/`_sum` members:
        a bound `X_bucket` stands for the histogram X."""
        ws = self.svc.ws
        bounded = bool(ws.catalog_bounded_by(source, metric))
        has = ws.catalog.has_metric
        if metric.endswith("_bucket"):
            base = metric.removesuffix("_bucket")
            if has(source, f"{base}_count") or ws.catalog_facts(source, metric).type in (
                "histogram", None,
            ):  # fmt: skip
                return MetricInfo(base, "histogram", False, bounded)
        mtype = ws.catalog_facts(source, metric).type
        # the suggestion's word, else czt.2's: the catalog family, then _sum/_count
        is_native = metric in native or (
            mtype == "histogram" and self.svc.littles.native_histogram(source, metric)
        )
        return MetricInfo(metric, mtype, is_native, bounded)

    def plans(
        self, source: str, b: Resolved, mt: Mapping[str, str], error_matcher: str | None
    ) -> tuple[dict[str, MetricInfo], dict[str, RolePlan]]:
        """Each filled role's metric facts and drawing plan (shared with binding_verdict)."""
        names = self.svc.ws.catalog.names(source, limit=100_000)
        infos = {r: self._info(source, m, b.native) for r, m in b.roles.items() if m}
        rate_role = {"RED": "rate", "littles_law": "arrival_rate"}.get(b.kind)
        plans: dict[str, RolePlan] = {}
        for role, info in infos.items():
            plans[role] = plan_role(
                b.kind, role, info, key=b.key, join_on=b.join_on, matchers=mt,
                hint=b.hints.get(role), rate_metric=infos.get(rate_role) if rate_role else None,
                error_matcher=error_matcher, names=names,
            )  # fmt: skip
        return infos, plans

    # the view -----------------------------------------------------------------------------------
    async def show(
        self,
        source: str = "default",
        kind: str | None = None,
        key: str | None = None,
        suggestion: str | None = None,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        matchers: Mapping[str, str] | None = None,
        error_matcher: str | None = None,
        actor: Actor = "claude",
        reframed_from: str | None = None,
    ) -> PanelGroup:
        svc = self.svc
        b = self.resolve(source, kind, key, suggestion)
        src = svc._source(source)
        rng = parse_range(start, end, svc.clock())
        mt = dict(matchers or {})
        infos, plans = self.plans(source, b, mt, error_matcher)
        step_ms = self.grid_step(rng, step, src.resolution_ms, plans.values())
        group = svc.ws.group_create(
            actor, kind=b.kind, key=b.key, source=source, start_ms=rng.start_ms,
            end_ms=rng.end_ms, step_ms=step_ms, basis=b.basis, binding_origin=b.origin,
            suggestion=b.suggestion, join_on=b.join_on, matchers=mt, roles=[],
            error_matcher=error_matcher, reframed_from=reframed_from,
        )  # fmt: skip
        when = {
            "start": str(rng.start_ms),
            "end": str(rng.end_ms),
            "step": format_duration(step_ms),
        }
        fetched = await asyncio.gather(
            *(self.fetch(source, p, when, actor) for p in plans.values()), return_exceptions=True
        )
        by_role = dict(zip(plans, fetched, strict=True))
        roles = [
            self._draw(group, plans[role], by_role[role], step_ms, actor)
            if role in plans
            else _gap_role(b, role)
            for role in BINDING_ROLES[b.kind]
        ]
        if b.kind == "littles_law" and set(BINDING_ROLES[b.kind]) <= infos.keys():
            roles.append(await self._littles(group, b, infos, mt, rng, actor))
        elif b.kind == "littles_law" and "concurrency" not in infos:
            # the check is not possible: say so where its panel would be, with the gauge to add
            g = _gap_role(b, "concurrency")
            roles.append(g.model_copy(update={
                "role": "check", "form": "littles",
                "why": f"{NOT_POSSIBLE}; L is not estimated. Instrument: {g.why}",
            }))  # fmt: skip
        await asyncio.gather(
            *(
                svc.y_context(r.panel, actor)
                for r in roles
                if r.panel and r.view in ("lines", "fleet")
            )
        )
        notes = (
            [f"join_on {', '.join(b.join_on)}: label names are conventions"] if b.join_on else []
        )
        group = svc.ws.objects.set_group(group.model_copy(update={"roles": roles, "notes": notes}))
        svc.log.append(actor, "panel_group.updated", group.id, {"roles": len(roles)})
        return group

    async def _littles(
        self,
        group: PanelGroup,
        b: Resolved,
        infos: Mapping[str, MetricInfo],
        mt: dict,
        rng: TimeRange,
        actor: Actor,
    ) -> GroupRole:
        """The model panel of a Little's law group: check_littles_law's L vs λ·W (czt.2) over
        the group's window, drawn with its own mark. The check is czt.2's; this only shows it."""
        svc = self.svc
        base: dict[str, Any] = {"role": "check", "form": "littles"}
        try:
            out = await self.check_littles(group.source, b, infos, mt, rng, actor)
            res = svc.show(
                out["datasets"]["concurrency"], f"Is {b.key}'s L consistent with λ·W?", actor,
                mark="littles", group=GroupRef(id=group.id, role="check"),
            )  # fmt: skip
        except ROLE_FAILURES as e:
            return GroupRole(**base, view="error", error=str(e))
        notes = [f"verdict: {out['verdict']} (check_littles_law, window {out['window']})"]
        return GroupRole(**base, panel=res.panel.id, view="model", notes=notes)

    async def check_littles(
        self,
        source: str,
        b: Resolved,
        infos: Mapping[str, MetricInfo],
        mt: Mapping[str, str],
        rng: TimeRange,
        actor: Actor,
    ) -> dict:
        """check_littles_law (czt.2) on a Little's law binding's metrics over `rng`, by its
        join_on (show_binding's model panel, binding_verdict's model_check)."""
        return await self.svc.check_littles_law(
            actor=actor, source=source, by=list(b.join_on),
            start=str(rng.start_ms), end=str(rng.end_ms), **littles_selectors(infos, mt),
        )  # fmt: skip

    @staticmethod
    def grid_step(rng: TimeRange, step: str, res_ms: int, plans: Iterable[RolePlan]) -> int:
        """One step for every role of a group (show_binding and binding_verdict)."""
        from telemetry_nerd.core.service import DIST_TARGET_COLUMNS, auto_step

        dist = any(p.form == "distribution" for p in plans)
        if step != "auto":
            s = parse_duration(step)
            if s <= 0:
                raise ValueError(f"step must be positive, got {step!r}")
            return s
        line = auto_step(rng, res_ms)
        # one step for every role so the panels line up: the distribution's (coarser) when there
        # is one, since increase() per column needs two scrapes
        return max(line, auto_step(rng, 2 * res_ms, DIST_TARGET_COLUMNS)) if dist else line

    async def fetch(self, source: str, p: RolePlan, when: dict, actor: Actor) -> dict:
        """One role's dataset, as planned, over `when` (start, end, step)."""
        svc = self.svc
        if p.unresolved:
            raise ValueError(p.unresolved)
        if p.form == "distribution":
            assert p.selector is not None
            ds = (
                await svc.query_distribution(p.selector, p.by, source=source, actor=actor, **when)
            )["dataset"]
            n = svc.datasets.series_count(ds)
            notes: list[str] = []
            if p.by and n > DIST_FACETS_MAX:
                ds = (
                    await svc.query_distribution(p.selector, (), source=source, actor=actor, **when)
                )["dataset"]
                notes.append(
                    f"{n} members merged into one distribution (histograms merge exactly); "
                    f"per member: query_distribution(by={list(p.by)})"
                )
            return {"dataset": ds, "members": n, "notes": notes}
        if p.form == "error_ratio":
            assert p.num is not None and p.den is not None
            num_ds, den_ds = await asyncio.gather(
                svc.query(p.num, source=source, actor=actor, **when),
                svc.query(p.den, source=source, actor=actor, **when),
            )
            nm, nr = svc.datasets.get(num_ds["dataset"])
            dm, dr = svc.datasets.get(den_ds["dataset"])
            result, notes = error_ratio(nr, dr, dm.step_ms)
            meta = svc.datasets.put(
                source=dm.source, expr=f"({nm.expr}) / ({dm.expr})",
                rng=TimeRange(dm.start_ms, dm.end_ms), step_ms=dm.step_ms,
                resolution_ms=dm.resolution_ms, result=result,
                lineage=Lineage(
                    producer={"kind": "binding", "op": "error_ratio",
                              "description": "errors / requests per step, Wilson interval"},
                    parents=(nm.id, dm.id), unit="ratio",
                    uncertainty={"method": RATIO_METHOD, "level": RATIO_LEVEL,
                                 "kind": "confidence"},
                ),
            )  # fmt: skip
            svc.log.append(actor, "dataset.created", meta.id, {"expr": meta.expr})
            return {"dataset": meta.id, "notes": notes}
        assert p.expr is not None
        ds = (await svc.query(p.expr, source=source, actor=actor, **when))["dataset"]
        return {"dataset": ds, "notes": []}

    def _draw(
        self, group: PanelGroup, p: RolePlan, got: Any, step_ms: int, actor: Actor
    ) -> GroupRole:
        from telemetry_nerd.core.service import ChartRejected

        svc = self.svc
        base: dict[str, Any] = {"role": p.role, "metric": p.metric, "form": p.form}
        if isinstance(got, BaseException):
            if not isinstance(got, ROLE_FAILURES):
                raise got
            return GroupRole(**base, view="error", error=str(got), notes=list(p.notes))
        ds = got["dataset"]
        notes = [*p.notes, *got.get("notes", [])]
        ref = GroupRef(id=group.id, role=p.role)
        try:
            if p.form == "distribution":
                res = svc.show(ds, p.question, actor, group=ref)
                return GroupRole(
                    **base, panel=res.panel.id, view="heatmap", members=got.get("members"),
                    notes=notes,
                )  # fmt: skip
            n = svc.datasets.series_count(ds)
            if n == 0:
                return GroupRole(
                    **base, view="error", notes=notes,
                    error=f"no data for {p.metric} in this window (expr: {svc.datasets.meta(ds).expr})",
                )  # fmt: skip
            mark = "fleet" if n > LINE_SERIES_BUDGET else "auto"
            kw: dict[str, Any] = {}
            if p.bounds is not None:
                kw = {
                    "bounds_lo": p.bounds[0],
                    "bounds_hi": p.bounds[1],
                    "bounds_by": "the RED errors role (a share of requests)",
                }
            elif p.form == "utilization":
                kw = self._utilization_bounds(ds)
            res = svc.show(
                ds, p.question, actor, unit=p.unit, mark=mark, raw_ok=True, group=ref, **kw
            )
        except (ValueError, LookupError, ChartRejected) as e:
            return GroupRole(**base, view="error", error=str(e), notes=notes)
        if mark == "fleet":
            notes.append(f"{n} members: drawn as a fleet (spread band, median, outliers)")
        return GroupRole(
            **base, panel=res.panel.id, view="fleet" if mark == "fleet" else "lines", members=n,
            notes=notes,
        )  # fmt: skip

    def _utilization_bounds(self, ds: str) -> dict[str, Any]:
        """A utilization is a share of capacity: its natural axis is 0..1 (or 0..100 in percent).
        The catalog/derivation rules decide when they can (y_context); otherwise the role does,
        if the data agree with it."""
        svc = self.svc
        meta, result = svc.datasets.get(ds)
        if svc._derived_bounds(meta) is not None:
            return {}
        st = value_stats(result.buckets, meta.representation, meta.n_min)
        if st.lo is None or st.hi is None:
            return {}
        bound = natural_bound(st.lo, st.hi, meta.expr)
        if bound is None:
            return {}
        by = "the USE utilization role (a share of capacity)"
        return {"bounds_lo": 0.0, "bounds_hi": bound, "bounds_by": by}
