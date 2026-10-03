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
from collections.abc import Callable
from typing import NamedTuple

import numpy as np
import polars as pl

from telemetry_nerd.analysis import sources
from telemetry_nerd.analysis.fleet import (
    ALPHA,
    DEFAULT_BAND_WINDOW,
    EXCURSIONS,
    MIN_MEMBERS,
    MIN_TESTED,
    P_BEYOND_3,
    POOL_HALF,
    WIDENING_ALPHA,
    ControlBand,
    Fleet,
    Outlier,
    analyse,
    check_band_window,
    control_band,
    flagged_steps,
    missing_bounds,
    trimmed_interval,
    trimmed_mean,
)
from telemetry_nerd.analysis.fleet_clusters import (
    MAX_CLUSTERS,
    MIN_CLUSTER,
    N_NULL,
    SPLIT_ALPHA,
    TEST_ALPHA,
    Groups,
    analyse_groups,
)
from telemetry_nerd.analysis.resample import lod
from telemetry_nerd.analysis.sources import COMMON, MEASUREMENT, SPECIAL, UNDETERMINED
from telemetry_nerd.catalog.rules import Facts
from telemetry_nerd.core.signal_ops import SignalOps
from telemetry_nerd.core.wire import Memo, measurement_caveats, sig, sig_list, statistic
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.bucket_state import Flag, State, coarsen, grid
from telemetry_nerd.model.caveats import (
    MAX_WHERE_SERIES,
    NO_REASON,
    Caveat,
    Where,
    failure_reasons,
    runs,
)
from telemetry_nerd.model.companions import dataset_bundle
from telemetry_nerd.model.time import TimeRange, format_duration, iso

MAX_STEPS = 1440  # longer ranges are averaged per member to a coarser step first
MAX_LISTED = 10
MAX_DRAWN = 6
CHURN_TOL = 0.05  # appeared / stopped: beyond max(3 steps, 5% of the window) from the edge
MISSING_WARN = 0.05  # members_missing / members_partial warn above this share of member-steps
_QUANTILE_EXPR = re.compile(r"\b(histogram_quantile|quantile_over_time)\s*\(", re.IGNORECASE)

MEMBER_ERROR_NOTE = (
    "member measurement error not propagated (principle 4): the band and the tests use each "
    "member's reported value as exact; per-member sampling or bucket error is not in sigma"
)

QUANTILE_REFUSAL = (
    "fleet refused on percentile series: the spread of member percentiles is not the fleet's "
    "percentile, and medians or bands of p99s aggregate percentiles (hint: compare members by a "
    "rate, or by the share of requests over a threshold per member with query_distribution + "
    "fraction_over, or by their histograms)"
)


class Coverage(NamedTuple):
    """The dataset's bucket_state laid on the fleet grid (lkn.13); None arrays without one."""

    state: np.ndarray | None  # members x steps State codes
    stale: np.ndarray | None  # members x steps: STALE_MARKER flag
    located: list[Caveat]  # structured caveats, where = {series ids, spans}


class FleetRun(NamedTuple):
    """One analysed fleet, as memoised by FleetOps.run."""

    meta: DatasetMeta
    step: int
    ts: list[int]
    labels: list[dict]
    names: list[str]
    fleet: Fleet
    caveats: list[str]
    groups: Groups | None  # behaviour groups (lkn.10)
    coverage: Coverage
    series_ids: list[str]


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
        self._memo: Memo[FleetRun] = Memo()
        self._last: dict[str, dict] = {}  # dataset -> options of the latest fleet() call

    def last_config(self, dataset_id: str) -> dict:
        """Options of the latest fleet() on this dataset; defaults when show() comes first."""
        default = {"by": None, "scale": "auto", "normalise": "none", "band_window": None}
        return dict(self._last.get(dataset_id, default))

    # preconditions ------------------------------------------------------------------
    def check(self, dataset_id: str) -> DatasetMeta:
        meta = self._datasets.meta(dataset_id)
        if meta.representation == "quantile" or _QUANTILE_EXPR.search(meta.expr):
            raise ValueError(QUANTILE_REFUSAL)
        self._signal.check(dataset_id, "fleet")  # distributions, raw counters
        return meta

    # analysis -----------------------------------------------------------------------
    def run(self, dataset_id: str, by: list[str] | None, scale: str, normalise: str) -> FleetRun:
        key = (dataset_id, tuple(by or ()), scale, normalise)
        if (hit := self._memo.get(key)) is not None:
            return hit
        self.check(dataset_id)
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
        cov = self._coverage(meta, result, sids, ts, step)
        unknown = None if cov.state is None else cov.state == State.UNKNOWN
        f = analyse(y, scale=scale, normalise=normalise, unknown=unknown)  # type: ignore[arg-type]
        groups = analyse_groups(y, f, labels, unknown)
        caveats = caveats + f.caveats
        if groups is not None:  # outliers are judged within each group; > 10% no longer holds
            caveats = [c for c in caveats if c != "many_outliers"] + ["clustered"]
        cov = cov._replace(located=cov.located + self._located(meta, f, cov, sids, ts, step))
        for c in cov.located:
            if c.severity != "info" and c.code not in caveats:
                caveats.append(c.code)
        res = FleetRun(meta, step, ts, labels, names, f, caveats, groups, cov, sids)
        self._memo.put(key, res)
        return res

    def _coverage(self, meta, result, sids: list[str], ts: list[int], step: int) -> Coverage:
        """bucket_state (series-bundles spec §5) on the fleet grid, coarsened like the values."""
        bundle = dataset_bundle(self._datasets, meta, result)
        states = bundle.companions.get("bucket_state")
        if states is None or not ts:
            return Coverage(None, None, list(bundle.caveats))
        if step != meta.step_ms:
            states = coarsen(states, step)
        df = pl.from_arrow(states)
        assert isinstance(df, pl.DataFrame)
        row = {sid: i for i, sid in enumerate(sids)}
        r = np.array([row.get(x, -1) for x in df["series_id"].to_list()], dtype=int)
        off = df["ts_ms"].to_numpy().astype(np.int64) - ts[0]
        c = off // step
        ok = (r >= 0) & (off % step == 0) & (c >= 0) & (c < len(ts))
        state = np.full((len(sids), len(ts)), int(State.EMPTY), np.uint8)
        state[r[ok], c[ok]] = df["state"].to_numpy()[ok]
        flags = np.zeros((len(sids), len(ts)), np.uint16)
        flags[r[ok], c[ok]] = df["flags"].to_numpy()[ok]
        return Coverage(state, (flags & int(Flag.STALE_MARKER)) > 0, list(bundle.caveats))

    @staticmethod
    def _located(meta, f: Fleet, cov: Coverage, sids, ts, step) -> list[Caveat]:
        """Fleet caveats that know where they apply: unknown spans (excluded from n and alive),
        partial buckets (fewer samples than expected: values from fewer samples), members missing
        (alive, no report)."""
        out: list[Caveat] = []
        sp = f.spread
        m_ = len(sids)

        def where(mask_ms: np.ndarray, members: list[int]) -> Where:
            spans = runs([ts[j] for j in np.flatnonzero(mask_ms)], step)
            ids = None if len(members) == m_ else [sids[i] for i in members[:MAX_WHERE_SERIES]]
            return Where(spans=spans, series=ids)

        def total(mask_ms: np.ndarray) -> str:
            return format_duration(int(mask_ms.sum()) * step)

        alive_cells = float(np.sum(sp.alive))
        if cov.state is not None:
            unk = cov.state == State.UNKNOWN
            # one caveat per set of members unknown at the same steps: a member is not marked
            # unknown where only another one was, and reasons are those touching its own steps
            by_steps: dict[bytes, list[int]] = {}
            for i in np.flatnonzero(unk.any(axis=1)):
                by_steps.setdefault(unk[i].tobytes(), []).append(int(i))
            for members in by_steps.values():
                steps = unk[members[0]]
                reasons = failure_reasons(
                    [(a, b, str(r)) for a, b, r in meta.failed_spans],
                    [ts[j] for j in np.flatnonzero(steps)],
                    step,
                ) or [NO_REASON]
                out.append(Caveat(
                    code="untrusted_data",
                    message=f"Data unknown for {total(steps)} ({'; '.join(reasons)}): those "
                    "member-steps are left out of n and alive, neither reporting nor missing.",
                    where=where(steps, members), source="bucket_state",
                ))  # fmt: skip
            part = cov.state == State.PARTIAL
            if part.any():
                share = float(part.sum()) / alive_cells if alive_cells else 0.0
                members = [int(i) for i in np.flatnonzero(part.any(axis=1))]
                out.append(Caveat(
                    code="members_partial",
                    severity="warn" if share > MISSING_WARN else "info",
                    message=f"{len(members)} of {m_} members reported fewer samples than expected "
                    f"in some buckets ({share:.1%} of member-steps): those values rest on fewer "
                    "samples.",
                    where=where(part.any(axis=0), members), source="bucket_state",
                ))  # fmt: skip
        missing = 1 - float(np.sum(sp.n)) / alive_cells if alive_cells else 0.0
        if missing > MISSING_WARN:
            seen = np.maximum.accumulate(~np.isnan(f.values), axis=1)
            silent = seen & np.isnan(f.values)
            if cov.state is not None:
                silent &= cov.state != State.UNKNOWN
            members = [int(i) for i in np.flatnonzero(silent.any(axis=1))]
            out.append(Caveat(
                code="members_missing",
                message=f"{len(members)} of {m_} members did not report at some steps "
                f"({missing:.1%} of alive member-steps); n per step counts those that did.",
                where=where(sp.n < sp.alive, members), source="fleet",
            ))  # fmt: skip
        return out

    def _check_units(self, meta: DatasetMeta, labels: list[dict]) -> None:
        names = sorted({lb["__name__"] for lb in labels if "__name__" in lb})
        if len(names) < 2 or meta.code_node:  # a code output has one declared unit
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
        band_window: int | None = None,
    ) -> dict:
        check_band_window(band_window)
        run = self.run(dataset_id, by, scale, normalise)
        f, ts, names, groups, cov = run.fleet, run.ts, run.names, run.groups, run.coverage
        self._last[dataset_id] = {
            "by": by, "scale": scale, "normalise": normalise, "band_window": band_window,
        }  # fmt: skip
        caveats = list(run.caveats)  # the memoised list stays as computed
        ranked = self.ranked_outliers(run)
        m_, t_ = f.z.shape
        sp = f.spread
        eff = format_duration(run.step)
        tol = max(3, math.ceil(CHURN_TOL * t_))
        # steps whose data is unknown (fetch failed) cannot show a member absent or silent
        known = np.ones((m_, t_), bool) if cov.state is None else cov.state != State.UNKNOWN
        appeared = [
            {"member": names[i], "first_seen": iso(ts[f.first_seen[i]])}
            for i in range(m_)
            if f.first_seen[i] >= tol and known[i, : f.first_seen[i]].sum() >= tol
        ]
        stopped = []
        for i in range(m_):
            last = f.last_seen[i]
            if not (0 <= last < t_ - tol and known[i, last + 1 :].sum() >= tol):
                continue
            item = {"member": names[i], "last_seen": iso(ts[last])}
            if cov.stale is not None and cov.stale[i, last:].any():
                item["state"] = (
                    "marked_stale"  # the source marked it stale (a marker, not a claim it left)
                )
            else:
                item["state"] = "silent"  # no samples since last_seen (source undetermined)
            stopped.append(item)
        never = sum(1 for i in range(m_) if f.first_seen[i] < 0)
        alive_sum = float(np.sum(sp.alive))
        missing = 1 - float(np.sum(sp.n)) / alive_sum if alive_sum else 0.0
        if missing > MISSING_WARN and "members_missing" not in caveats:
            caveats.append("members_missing")
        for a in appeared:
            a["source"] = MEASUREMENT  # the fleet measured changes membership: n moves
        for st in stopped:  # marked_stale: membership; silent: the data cannot tell the source
            st["source"] = MEASUREMENT if st["state"] == "marked_stale" else UNDETERMINED
        churn: dict = {"appeared": appeared[:MAX_LISTED], "stopped_reporting": stopped[:MAX_LISTED]}
        if len(appeared) > MAX_LISTED or len(stopped) > MAX_LISTED:
            churn["counts"] = {"appeared": len(appeared), "stopped_reporting": len(stopped)}
        if any(s["state"] == "silent" for s in stopped):
            churn["note"] = (
                "a silent member has no values since last_seen; this says nothing about a staleness "
                "marker (our range queries cannot show markers, so absence of one here is not "
                "evidence that none was written), so the data cannot tell why; it "
                "may have been replaced, or may return; check them"
                + (" - as many appeared, possibly replacement" if appeared and abs(len(appeared) - len(stopped)) <= 1 else "")
            )  # fmt: skip
        listed = [
            _labelled(
                self._outlier(dataset_id, eff, gf, o, gn, gl, ts) | extra | self._partial(cov, gi, o), o
            )
            for gf, o, gn, gl, extra, gi in ranked[:MAX_LISTED]
        ]  # fmt: skip
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
            "band": self._band_overall(run, band_window),
            "spread": self._spread_summary(f, ts),
            "coverage": {
                "n_per_step": {"min": int(sp.n.min()), "median": float(np.median(sp.n)),
                               "max": int(sp.n.max())},
                "alive_max": int(sp.alive.max()), "missing_share": sig(missing, 3),
            },
            "churn": churn,
            "outliers": listed,
            "tests": self._tests(f),
            "caveats": caveats,
            "located": [c.model_dump() for c in cov.located],
            "draw": f'show("{dataset_id}", question, mark="fleet")',
        }  # fmt: skip
        if groups is not None:
            out["clusters"] = self._clusters(f, groups, names, run.labels)
            for item, (_, gf, gb) in zip(
                out["clusters"]["groups"], self.bands(run, band_window), strict=True
            ):
                item["band"] = self._band_summary(gf, gb, ts)
        if len(ranked) > MAX_LISTED:
            out["more_outliers"] = len(ranked) - MAX_LISTED
        if f.departing:  # named, not judged: no noise scale exists to judge them by
            out["members"]["departing_from_constant"] = {
                "members": [names[i] for i in f.departing[:MAX_LISTED]],
                "note": "every other member reports one value at every step (e.g. 0 errors): "
                "these depart from it; with no spread among the others there is no noise scale "
                "to test the departure against, so they are named here, not judged as outliers",
            }
        out["variation"] = self._variation(out, caveats + measurement_caveats(run.meta))
        return out

    @staticmethod
    def _variation(out: dict, caveats: list[str]) -> list[dict]:
        """The fleet's labelled findings in one list (spec §5.4)."""
        var = [sources.item(
            COMMON, "the fleet's SPC band (per-step median +- 2 / 3 pooled robust sigma, the "
            "reference the outlier tests use) and its per-step spread across members "
            "(descriptive: the fleet is the population): the common-cause envelope",
        )]  # fmt: skip
        if cl := out.get("clusters"):
            var.append(sources.item(
                COMMON, f"{cl['k']} behaviour groups: systemic structure (strata of the fleet, "
                "e.g. sizes or placements); act on the configuration, not on members",
            ))  # fmt: skip
        var += [
            sources.item(o["source"], f"{o['member']}: {o['kind']} outlier, {o['direction']}",
                         member=o["member"])
            for o in out["outliers"]
        ]  # fmt: skip
        churn = out["churn"]
        var += [
            sources.item(a["source"], f"{a['member']} appeared {a['first_seen']} (normal lifecycle, not a fault: n moves)", member=a["member"])
            for a in churn["appeared"]
        ]  # fmt: skip
        var += [
            sources.item(st["source"], f"{st['member']} stopped reporting {st['last_seen']} "
                         f"({st['state']})", member=st["member"])
            for st in churn["stopped_reporting"]
        ]  # fmt: skip
        return var + sources.measurement_items(caveats)

    @staticmethod
    def ranked_outliers(
        run: FleetRun,
    ) -> list[tuple[Fleet, Outlier, list[str], list[dict], dict, int]]:
        """Outliers by score: the fleet's, or when it is split into behaviour groups, each
        group's (judged against its own group) tagged with the group id. Each item: (the fleet
        the outlier was judged in, outlier, member names and labels of that fleet, extra keys,
        the member's index in the whole fleet)."""
        f, names, labels, groups = run.fleet, run.names, run.labels, run.groups
        if groups is None:
            return [(f, o, names, labels, {}, o.member) for o in f.outliers]
        out = []
        for k, g in enumerate(groups.groups):
            gn = [names[i] for i in g.members]
            gl = [labels[i] for i in g.members]
            out += [
                (g.fleet, o, gn, gl, {"cluster": f"c{k + 1}"}, g.members[o.member])
                for o in g.fleet.outliers
            ]
        out.sort(key=lambda r: -r[1].score)
        return out

    @staticmethod
    def _partial(cov: Coverage, gi: int, o: Outlier) -> dict:
        """Partial buckets of an outlier (values from fewer samples than expected), overall and
        within each episode: an excursion that coincides with partial buckets is suspect."""
        if cov.state is None:
            return {}
        part = cov.state[gi] == State.PARTIAL
        if not part.any():
            return {}
        out: dict = {"partial_buckets": int(part.sum())}
        eps = [int(part[e.start : e.end + 1].sum()) for e in o.episodes[:5]]
        if any(eps):
            out["episode_partial_buckets"] = eps
        return out

    @staticmethod
    def _clusters(f: Fleet, groups: Groups, names: list[str], labels: list[dict]) -> dict:
        c = groups.clustering
        log = f.scale == "log"
        best = groups.explained_by[0] if groups.explained_by else None
        items = []
        for k, g in enumerate(groups.groups):
            gid = f"c{k + 1}"
            lv = math.exp(g.level) if log else g.level
            item: dict = {
                "id": gid, "size": len(g.members),
                "members": [names[i] for i in g.members[:MAX_LISTED]],
                "level_vs_fleet": {"ratio" if log else "difference": sig(lv, 3)},
                "tested": len(g.fleet.tested), "outliers": len(g.fleet.outliers),
                "caveats": g.fleet.caveats,
            }  # fmt: skip
            if len(g.members) > MAX_LISTED:
                item["more_members"] = len(g.members) - MAX_LISTED
            if best is not None:
                item["label_values"] = {best["label"]: best["values"].get(k, [])}
            items.append(item)
        out: dict = {
            "k": len(groups.groups),
            "rule": (
                "recursive 2-means on each member's deviation from the fleet (20% trimmed means "
                f"over {c.blocks} time blocks: level and shape); a split is kept when its cluster "
                "index (within / total SS) is below a single Gaussian's with the same covariance "
                f"(SigClust, up to {N_NULL} seeded simulations, each test at p < {sig(TEST_ALPHA, 2)}: "
                f"{SPLIT_ALPHA} family-wise over the splits) and both parts have "
                f">= {MIN_CLUSTER} members; at most {MAX_CLUSTERS} groups. Each group is analysed "
                f"as its own fleet at family-wise {ALPHA}/k"
            ),
            "split_tests": [
                {
                    "sizes": list(t.sizes),
                    "cluster_index": sig(t.ci, 3),
                    "null_cluster_index_median": sig(t.null_ci_median, 3),
                    "p": sig(t.p, 3),
                    "accepted": t.accepted,
                    "simulations": t.simulations,
                }
                for t in c.tests
            ],
            "groups": items,
            "fleet_level_outliers": len(f.outliers),
            "explained_by": [
                {"label": e["label"], "adjusted_rand": sig(e["ari"], 3)}
                for e in groups.explained_by[:3]
            ],
        }
        if not groups.explained_by:
            out["note"] = (
                "no label separates the groups (adjusted Rand >= 0.5): the difference is in "
                "behaviour only; look for a configuration or placement the labels do not carry"
            )
        if groups.assigned_by_label:
            out["assigned_by_label"] = [names[i] for i in groups.assigned_by_label[:MAX_LISTED]]
        if groups.unassigned:
            out["unassigned"] = len(groups.unassigned)
        return out

    @staticmethod
    def _scale_text(f: Fleet) -> str:
        if f.scale == "log":
            return "log: members compared by ratio (all values > 0)"
        return "linear: members compared by difference"

    @staticmethod
    def bands(run: FleetRun, window: int | None) -> list[tuple[str | None, Fleet, ControlBand]]:
        """The SPC band(s) outliers were judged against: the whole fleet's, or when it is split
        into behaviour groups each group's own (its centre, sigma and flag threshold). Member-
        steps the tests flagged are excluded from the beyond-3-sigma counts."""
        if run.groups is None:
            f = run.fleet
            flagged = flagged_steps(f.z.shape, [(o.member, o) for o in f.outliers])
            return [(None, f, control_band(f, window, flagged))]
        out = []
        for k, g in enumerate(run.groups.groups):
            gf = g.fleet
            flagged = flagged_steps(gf.z.shape, [(o.member, o) for o in gf.outliers])
            out.append((f"c{k + 1}", gf, control_band(gf, window, flagged)))
        return out

    @staticmethod
    def band_text(b: ControlBand) -> str:
        unit = "log scale: multiplicative" if b.scale == "log" else "linear scale"
        return f"median ± 2σ/3σ (robust, pooled ±{b.pool_half} steps, {unit})"

    @staticmethod
    def unflagged_text(f: Fleet) -> str:
        k = len(f.tested)
        if k < MIN_TESTED:
            return f"outside 3σ; no outlier tests ran ({k} members tested, {MIN_TESTED} needed)"
        return f"outside 3σ, not significant at fleet-wide {ALPHA:.0%} ({k} members tested)"

    @staticmethod
    def heavy_note(f: Fleet) -> str:
        return (
            "; heavy-tailed noise: more than 0.27% beyond 3σ is this fleet's shape, not a fault"
            if "heavy_tailed_noise" in f.caveats
            else ""
        )

    @staticmethod
    def pct(v: float) -> str:
        return f"{100 * v:.2g}%"

    def count_text(self, f: Fleet, b: ControlBand) -> str:
        return (
            f"{b.outside3_total} member-steps beyond 3σ unflagged ({self.pct(b.outside3_rate)}; "
            f"0.27% if normal){self.heavy_note(f)}"
        )

    @staticmethod
    def widening_rule(b: ControlBand) -> str:
        return (
            f"p-chart of the per-step count of unflagged members beyond 3σ against this window's "
            f"own share ({FleetOps.pct(b.p_hat)}, never below 0.27%), quasi-binomial with "
            f"overdispersion φ = {b.phi:.2g}, family-wise {WIDENING_ALPHA:.0%} over the steps: "
            "the chance of any mark in this window ≈ 1%"
        )

    def widening_text(self, b: ControlBand) -> str:
        n = int(b.widening.sum())
        return (
            f"{n} step{'s' if n != 1 else ''} where the fleet widened faster than the "
            f"±{POOL_HALF}-step σ tracks (common cause: the whole fleet, not a member)"
        )

    @staticmethod
    def threshold_note() -> str:
        return (
            "approximate: the tests' per-member bar varies with pooling (and the tests use "
            "leave-one-out sigma up to 64 members); level, change and episode tests flag "
            "members whose single steps stay inside it"
        )

    def _band_summary(self, f: Fleet, b: ControlBand, ts: list[int]) -> dict:
        log = f.scale == "log"
        s = b.sigma[np.isfinite(b.sigma)]
        smooth = b.window > DEFAULT_BAND_WINDOW
        out: dict = {
            "basis": self.band_text(b)
            + (f"; centre smoothed by a centred {b.window}-step moving median" if smooth else ""),
            "window_steps": b.window,
            "role": "stable SPC reference: the centre and sigma the outlier tests use; zones are "
            "for reading, flags come only from the family-wise tests",
            "source": COMMON,
            "caveat": MEMBER_ERROR_NOTE,
        }  # fmt: skip
        if smooth:
            out["smoothing"] = (
                f"centred, so no phase lag mid-window, but a fleet-wide step is spread over "
                f"±{b.pool_half} steps and fleet-wide peaks shorter than that are cut; in the "
                f"last {b.pool_half} steps the median is one-sided and lags"
            )
        if s.size:
            med = float(np.median(s))
            out["sigma_median"] = (
                {"ratio": sig(math.exp(med), 3)} if log else {"difference": sig(med, 3)}
            )
        if b.threshold_z is not None:
            out["flag_threshold_z"] = sig(b.threshold_z, 3)
            out["flag_line"] = "single-step (spike) bar drawn dashed; " + self.threshold_note()
        out["outside_3sigma_unflagged"] = {
            "member_steps": b.outside3_total, "members": b.outside3_members,
            "expected_if_normal": sig(b.expected3, 3), "cells": b.cells,
            "rate": sig(b.outside3_rate, 3), "rate_if_normal": sig(P_BEYOND_3, 3),
            "note": self.count_text(f, b), "each": self.unflagged_text(f),
        }  # fmt: skip
        out["widening"] = self._widening(b, ts)
        return out

    def _widening(self, b: ControlBand, ts: list[int], group: str | None = None) -> dict:
        wide = np.flatnonzero(b.widening)
        out: dict = {
            "steps": int(wide.size), "at": [iso(ts[j]) for j in wide[:MAX_LISTED]],
            "note": self.widening_text(b), "rule": self.widening_rule(b), "source": COMMON,
        }  # fmt: skip
        if group is not None:
            out["group"] = group
        return out

    def _band_overall(self, run: FleetRun, window: int | None) -> dict:
        bands = self.bands(run, window)
        if run.groups is None:
            _, f, b = bands[0]
            return self._band_summary(f, b, run.ts)
        b = bands[0][2]
        return {
            "basis": f"per behaviour group: each group's own {self.band_text(b)}, its own sigma and "
            "flag threshold (clusters.groups[].band)",
            "window_steps": b.window, "source": COMMON, "caveat": MEMBER_ERROR_NOTE,
            "outside_3sigma_unflagged": {
                "member_steps": (tot := sum(x.outside3_total for _, _, x in bands)),
                "expected_if_normal": sig(sum(x.expected3 for _, _, x in bands), 3),
                "rate": sig(tot / max(1, sum(x.cells for _, _, x in bands)), 3),
                "rate_if_normal": sig(P_BEYOND_3, 3),
            },
            "widening": [self._widening(x, run.ts, gid) for gid, _, x in bands],
        }  # fmt: skip

    @staticmethod
    def _spread_summary(f: Fleet, ts: list[int]) -> dict:
        sp = f.spread
        with np.errstate(invalid="ignore", divide="ignore"):
            rel = sp.q75 / sp.q25 if f.scale == "log" else sp.q75 - sp.q25
        ok = np.isfinite(rel)
        if not ok.any():
            return {"basis": "fewer than 5 members per step: no quartiles", "source": COMMON}
        third = max(1, len(ts) // 3)
        first, last = rel[:third], rel[-third:]
        widest = int(np.nanargmax(np.where(ok, rel, -np.inf)))
        kind = "q75 / q25 across members" if f.scale == "log" else "q75 - q25 across members"
        short = int(np.sum(sp.n < sp.alive))
        return {
            "basis": f"{kind} per step (descriptive: the fleet is the population; the panel's "
            "quantile view)",
            "missing_member_bounds": (
                f"{short} steps had alive members not reporting: there each quantile is drawn "
                "with bounds (missing values at -inf / +inf; unbounded when they reach its rank; "
                "min / max unknown beyond on the missing side)"
                if short
                else "every alive member reported at every step: no bounds needed"
            ),
            "caveat": MEMBER_ERROR_NOTE,
            "median": sig(float(np.nanmedian(rel))),
            "first_third": sig(float(np.nanmedian(first))) if np.isfinite(first).any() else None,
            "last_third": sig(float(np.nanmedian(last))) if np.isfinite(last).any() else None,
            "widest": {"at": iso(ts[widest]), "value": sig(float(rel[widest]))},
            "source": COMMON,  # spec §5.4: the fleet's own spread is the common-cause envelope
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
            out["member_threshold_z"] = sig(th["member"], 3)
            out["autocorrelation_time_steps"] = sig(th["tau"], 3)
            out["excursion_thresholds_z"] = {
                s: sig(th.get(f"{s}_tail_threshold", th[f"{s}_threshold"]), 3) for s in EXCURSIONS
            }
            out["excursion_widths_steps"] = dict(EXCURSIONS)
            if "phi" in th:
                out["episode_scan"] = (
                    f"rolling medians of AR(1)-prewhitened deviations (phi = {sig(th['phi'], 2)})"
                )
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
                return {"ratio": sig(math.exp(v)), "interval_99": [sig(math.exp(iv[0])), sig(math.exp(iv[1]))]}  # fmt: skip
            return {"difference": sig(v), "interval_99": [sig(iv[0]), sig(iv[1])]}

        def evidence(name: str, v: float, iv: tuple[float, float], method: str, **params) -> dict:
            val, lo, hi = (math.exp(v), math.exp(iv[0]), math.exp(iv[1])) if log else (v, *iv)
            return statistic(
                dataset_id, name, sig(val), [sig(lo), sig(hi)], method,
                {"member": names[o.member], "step": eff, "members": len(names), "n": o.n,
                 "tau": sig(o.tau, 3), "scale": f.scale, **params},
            )  # fmt: skip

        item: dict = {
            "member": names[o.member], "labels": labels[o.member], "kind": o.kind,
            "direction": o.direction, "score": sig(o.score, 3), "tests": o.fired,
            "z": {k: sig(v, 3) for k, v in o.z.items()},
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
        if o.kind != "transient":
            if o.episodes:  # both modes: excursions beyond its own level
                item["episodes"] = [
                    {"start": iso(ts[e.start]), "end": iso(ts[e.end]), "steps": e.end - e.start + 1,
                     "peak_z_beyond_own_level": sig(e.peak_z, 3), "sustained": e.sustained,
                     "calibrated": False}
                    for e in o.episodes[:5]
                ]  # fmt: skip
                item["episodes_note"] = (
                    "excursions beyond the member's own level: extra scans on a flagged member, "
                    "not calibrated and outside the family-wise budget (bead db0); a lead, not "
                    "a finding"
                )
        else:
            eps = []
            for e in o.episodes[:5]:
                eps.append({"start": iso(ts[e.start]), "end": iso(ts[e.end]),
                            "steps": e.end - e.start + 1, "peak_z": sig(e.peak_z, 3),
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
                    start=iso(ts[main.start]), end=iso(ts[main.end]), peak_z=sig(main.peak_z, 3),
                )  # fmt: skip
        return item

    @staticmethod
    def _effect(f: Fleet, o: Outlier, ts: list[int], step: int) -> dict:
        """What the end label says: the offset (persistent), the change and where it happened
        (shifted), the rate (drifting), as a ratio (log scale) or a difference."""
        log = f.scale == "log"
        conv = math.exp if log else (lambda v: v)
        out: dict = {"as": "ratio" if log else "difference", "offset": sig(conv(o.offset), 3)}
        out["over"] = "window"  # the offset is the window's 20% trimmed mean
        if o.kind == "persistent" and o.since is not None and not o.since_window_start:
            dev = f.d[o.member, o.since :]
            dev = dev[np.isfinite(dev)]
            if dev.size >= 3:  # what the label says "since": the offset over that stretch
                out["offset"] = sig(conv(float(trimmed_mean(dev[None, :])[0])), 3)
                out["over"] = "since"
        if o.kind in ("shifted", "drifting"):
            out["change"] = sig(conv(o.change), 3)
        if o.kind == "shifted" and o.at is not None:
            out["at_ms"] = ts[o.at]
        if o.kind == "drifting":
            t_ = len(ts)
            hours = (t_ - max(1, t_ // 3)) * step / 3_600_000  # between the thirds' centres
            if hours > 0:
                out["change_per_hour"] = sig(conv(o.change / hours), 3)
        return out

    # panel payload --------------------------------------------------------------------
    def panel(self, dataset_id: str, cfg: dict) -> dict:
        run = self.run(
            dataset_id, cfg.get("by"), cfg.get("scale", "auto"), cfg.get("normalise", "none")
        )
        f, ts, groups, cov = run.fleet, run.ts, run.groups, run.coverage
        sp = f.spread
        ranked = self.ranked_outliers(run)
        window = cfg.get("band_window")
        bands = self.bands(run, window)
        drawn = []
        for gf, o, gn, gl, extra, gi in ranked[:MAX_DRAWN]:
            drawn.append({
                "id": gn[o.member], "labels": gl[o.member], "kind": o.kind,
                "source": outlier_source(o, self._partial(cov, gi, o)),
                "direction": o.direction, "score": sig(o.score, 3),
                "values": sig_list(gf.values[o.member]),
                "since_ms": ts[o.since] if o.since is not None else None,
                "since_window_start": o.since_window_start,
                "episodes": [
                    {"start_ms": ts[e.start], "end_ms": ts[e.end], "peak_z": sig(e.peak_z, 3),
                     "sustained": e.sustained, "beyond_own_level": e.beyond_own_level}
                    for e in o.episodes
                ],
                "effect": self._effect(gf, o, ts, run.step),
                **extra,
            })  # fmt: skip
        payload = {
            "kind": "fleet",
            "effective_step_ms": run.step,
            "ts": ts,
            "members": len(run.names),
            "normalise": f.normalise,
            "scale": f.scale,
            "band": {
                k: sig_list(getattr(sp, k))
                for k in ("median", "q25", "q75", "q10", "q90", "lo", "hi")
            },
            "n": [int(v) for v in sp.n],
            "alive": [int(v) for v in sp.alive],
            "outliers": drawn,
            "outlier_count": len(ranked),
            "caveats": list(run.caveats),
            "located": [c.model_dump() for c in cov.located],
            "member_error_note": MEMBER_ERROR_NOTE,
        }
        if (bb := self._bounds_payload(f, cov)) is not None:
            payload["band_bounds"] = bb
        if groups is None:
            payload["spc"] = self._spc_payload(bands[0][1], bands[0][2])
        else:  # per-group bands (lkn.10), each group's own SPC reference (nq6)
            payload["clusters"] = [
                {"id": gid, "size": len(g.members),
                 "band": {q: sig_list(getattr(g.fleet.spread, q)) for q in ("median", "q25", "q75")},
                 "spc": self._spc_payload(gf, gb)}
                for (gid, gf, gb), g in zip(bands, groups.groups, strict=True)
            ]  # fmt: skip
        return payload

    def _spc_payload(self, f: Fleet, b: ControlBand) -> dict:
        thr = b.threshold_lo is not None and b.threshold_hi is not None
        return {
            "centre": sig_list(b.centre), "lo2": sig_list(b.lo2), "hi2": sig_list(b.hi2),
            "lo3": sig_list(b.lo3), "hi3": sig_list(b.hi3),
            "threshold_z": sig(b.threshold_z, 3) if b.threshold_z is not None else None,
            "threshold_lo": sig_list(b.threshold_lo) if thr else None,
            "threshold_hi": sig_list(b.threshold_hi) if thr else None,
            "threshold_note": self.threshold_note(),
            "window": b.window, "pool_half": b.pool_half, "legend": self.band_text(b),
            "outside3_total": b.outside3_total, "outside3_expected": sig(b.expected3, 3),
            "outside3_rate": sig(b.outside3_rate, 3), "outside3_cells": b.cells,
            "heavy_tails": "heavy_tailed_noise" in f.caveats,
            "outside3_count": self.count_text(f, b), "outside3_note": self.unflagged_text(f),
            "widening": [int(j) for j in np.flatnonzero(b.widening)],
            "widening_note": f"the fleet widened here faster than the ±{POOL_HALF}-step σ tracks "
            "(common cause)",
            "widening_rule": self.widening_rule(b),
            "tested": len(f.tested),
        }  # fmt: skip

    @staticmethod
    def _bounds_payload(f: Fleet, cov: Coverage) -> dict | None:
        """Missing-member bounds at the steps with members missing only (sparse); None when
        every alive member reported. Members the source marked stale count as gone, not
        missing, from their stale point on (principle 9: a positive observation)."""
        sp = f.spread
        m_, t_ = f.values.shape
        gone = np.zeros(t_, int)
        if cov.stale is not None:
            for i in range(m_):
                last = int(f.last_seen[i])
                if last < 0:
                    continue
                st = np.flatnonzero(cov.stale[i, last:])
                if st.size:
                    gone[max(last + 1, last + int(st[0])) :] += 1
        missing = np.maximum(sp.alive - sp.n - gone, 0)
        steps = np.flatnonzero(missing > 0)
        if not steps.size:
            return None
        bounds = missing_bounds(f.values, sp.n, sp.alive, gone)
        # values None where unbounded (+-inf) or the quantile is not drawn (UI: quantile drawn)
        out: dict = {"steps": [int(j) for j in steps], "missing": [int(missing[j]) for j in steps]}
        out |= {k: sig_list(v[steps]) for k, v in bounds.items()}
        return out


def outlier_source(o: Outlier, partial: dict) -> str:
    """An outlying member is a special cause (assignable: that member differs), unless what
    makes it one rests on partial buckets (values from fewer samples than expected): a
    transient whose episodes, or another kind whose buckets are half or more, are partial
    cannot be told from the measurement system (spec §5.4)."""
    if o.kind == "transient":
        return UNDETERMINED if any(partial.get("episode_partial_buckets", [])) else SPECIAL
    return UNDETERMINED if 2 * partial.get("partial_buckets", 0) >= max(o.n, 1) else SPECIAL


def _labelled(item: dict, o: Outlier) -> dict:
    src = outlier_source(o, item)
    item["source"] = src
    if "evidence" in item:
        item["evidence"]["source"] = src
    return item
