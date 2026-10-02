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

from telemetry_nerd.analysis.fleet import (
    ALPHA,
    EXCURSIONS,
    MIN_MEMBERS,
    Fleet,
    Outlier,
    analyse,
    trimmed_interval,
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
from telemetry_nerd.catalog.rules import Facts
from telemetry_nerd.core.signal_ops import SignalOps
from telemetry_nerd.core.wire import Memo, sig, sig_list, statistic
from telemetry_nerd.datasets.store import DatasetMeta, DatasetStore
from telemetry_nerd.model.bucket_state import Flag, State, coarsen, grid
from telemetry_nerd.model.caveats import Caveat, Where, runs
from telemetry_nerd.model.companions import dataset_bundle
from telemetry_nerd.model.time import TimeRange, format_duration, iso

MAX_STEPS = 1440  # longer ranges are averaged per member to a coarser step first
MAX_LISTED = 10
MAX_DRAWN = 6
CHURN_TOL = 0.05  # appeared / stopped: beyond max(3 steps, 5% of the window) from the edge
MISSING_WARN = 0.05  # members_missing / members_partial warn above this share of member-steps
MAX_WHERE_SERIES = 50  # located caveats name at most this many series (the message counts all)
_QUANTILE_EXPR = re.compile(r"\b(histogram_quantile|quantile_over_time)\s*\(", re.IGNORECASE)

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
    """One analysed fleet. A tuple (index access stays as before: meta, step, ts, labels, names,
    fleet, caveats); `groups` (behaviour groups, lkn.10) is appended."""

    meta: DatasetMeta
    step: int
    ts: list[int]
    labels: list[dict]
    names: list[str]
    fleet: Fleet
    caveats: list[str]
    groups: Groups | None = None
    coverage: Coverage | None = None
    series_ids: list[str] | None = None


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
        return dict(self._last.get(dataset_id, {"by": None, "scale": "auto", "normalise": "none"}))

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
            if unk.any():
                steps = unk.any(axis=0)
                members = [int(i) for i in np.flatnonzero(unk.any(axis=1))]
                reasons = sorted({str(x[2]) for x in meta.failed_spans}) or [
                    "source could not tell"
                ]
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
        run = self.run(dataset_id, by, scale, normalise)
        _, step, ts, labels, names, f, caveats, groups, cov, _sids = run
        self._last[dataset_id] = {"by": by, "scale": scale, "normalise": normalise}
        caveats = list(caveats)  # the memoised list stays as computed
        ranked = self.ranked_outliers(run)
        m_, t_ = f.z.shape
        sp = f.spread
        eff = format_duration(step)
        tol = max(3, math.ceil(CHURN_TOL * t_))
        # steps whose data is unknown (fetch failed) cannot show a member absent or silent
        known = np.ones((m_, t_), bool) if cov is None or cov.state is None else (
            cov.state != State.UNKNOWN)  # fmt: skip
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
            if cov is not None and cov.stale is not None and cov.stale[i, last:].any():
                item["state"] = "ended"  # the source marked it stale: target or series went away
            else:
                item["state"] = "silent"  # alive, no samples (spec §5.2): ended or sick
            stopped.append(item)
        never = sum(1 for i in range(m_) if f.first_seen[i] < 0)
        alive_sum = float(np.sum(sp.alive))
        missing = 1 - float(np.sum(sp.n)) / alive_sum if alive_sum else 0.0
        if missing > MISSING_WARN and "members_missing" not in caveats:
            caveats.append("members_missing")
        churn: dict = {"appeared": appeared[:MAX_LISTED], "stopped_reporting": stopped[:MAX_LISTED]}
        if len(appeared) > MAX_LISTED or len(stopped) > MAX_LISTED:
            churn["counts"] = {"appeared": len(appeared), "stopped_reporting": len(stopped)}
        if any(s["state"] == "silent" for s in stopped):
            churn["note"] = (
                "a silent member stopped without a staleness marker in the data (range queries do "
                "not carry them), so the data cannot tell whether it ended (replaced) or went "
                "silent (the sick one): check them"
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
                "alive_max": int(sp.alive.max()), "missing_share": sig(missing, 3),
            },
            "churn": churn,
            "outliers": [self._outlier(dataset_id, eff, gf, o, gn, gl, ts)
                         | extra | self._partial(cov, gi, o)
                         for gf, o, gn, gl, extra, gi in ranked[:MAX_LISTED]],
            "tests": self._tests(f),
            "caveats": caveats,
            "located": [c.model_dump() for c in cov.located] if cov is not None else [],
            "draw": f'show("{dataset_id}", question, mark="fleet")',
        }  # fmt: skip
        if groups is not None:
            out["clusters"] = self._clusters(f, groups, names, labels)
        if len(ranked) > MAX_LISTED:
            out["more_outliers"] = len(ranked) - MAX_LISTED
        return out

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
    def _partial(cov: Coverage | None, gi: int, o: Outlier) -> dict:
        """Partial buckets of an outlier (values from fewer samples than expected), overall and
        within each episode: an excursion that coincides with partial buckets is suspect."""
        if cov is None or cov.state is None:
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
            "median": sig(float(np.nanmedian(rel))),
            "first_third": sig(float(np.nanmedian(first))) if np.isfinite(first).any() else None,
            "last_third": sig(float(np.nanmedian(last))) if np.isfinite(last).any() else None,
            "widest": {"at": iso(ts[widest]), "value": sig(float(rel[widest]))},
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

    # panel payload --------------------------------------------------------------------
    def panel(self, dataset_id: str, cfg: dict) -> dict:
        run = self.run(
            dataset_id, cfg.get("by"), cfg.get("scale", "auto"), cfg.get("normalise", "none")
        )
        _, step, ts, _labels, names, f, caveats, groups, cov, _sids = run
        caveats = list(caveats)
        sp = f.spread
        ranked = self.ranked_outliers(run)
        drawn = []
        for gf, o, gn, gl, extra, _gi in ranked[:MAX_DRAWN]:
            drawn.append({
                "id": gn[o.member], "labels": gl[o.member], "kind": o.kind,
                "direction": o.direction, "score": sig(o.score, 3),
                "values": sig_list(gf.values[o.member]),
                "since_ms": ts[o.since] if o.since is not None else None,
                "episodes": [[ts[e.start], ts[e.end]] for e in o.episodes],
                **extra,
            })  # fmt: skip
        payload = {
            "kind": "fleet",
            "effective_step_ms": step,
            "ts": ts,
            "members": len(names),
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
            "caveats": caveats,
            "located": [c.model_dump() for c in cov.located] if cov is not None else [],
        }
        if groups is not None:  # per-group bands (additive; lkn.10)
            payload["clusters"] = [
                {"id": f"c{k + 1}", "size": len(g.members),
                 "band": {b: sig_list(getattr(g.fleet.spread, b)) for b in ("median", "q25", "q75")}}
                for k, g in enumerate(groups.groups)
            ]  # fmt: skip
        return payload
