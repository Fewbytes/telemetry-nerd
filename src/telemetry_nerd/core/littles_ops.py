"""check_littles_law: resolve the three signals, fetch them on one grid, judge L vs lambda W per
window and group, state the assumptions (czt.2). The statistics live in analysis.littles.

Design: docs/superpowers/specs/2026-10-02-littles-law-design.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import numpy as np
import polars as pl

from telemetry_nerd.analysis import sources
from telemetry_nerd.analysis.exprkind import rate_interval_ms
from telemetry_nerd.analysis.littles import (
    MIN_SPREAD,
    PEAK_COMMON,
    PEAK_UNDETERMINED,
    PROMOTE_ALPHA,
    Block,
    GroupResult,
    Substeps,
    check,
    combine,
    window_of,
)
from telemetry_nerd.analysis.sources import COMMON, MEASUREMENT, SPECIAL, UNDETERMINED
from telemetry_nerd.catalog.models import native_family
from telemetry_nerd.catalog.relations import SUGGESTIONS
from telemetry_nerd.core.uncertainty import mark_statistics
from telemetry_nerd.core.wire import Memo, sig, sig_pair, statistic
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.model.time import TimeRange, format_duration, iso, parse_duration, parse_time

#: windows aimed for over the range when `window` is auto
TARGET_WINDOWS = 12
MIN_SUBSTEPS_PER_WINDOW = 8
SUBSTEPS_PER_WINDOW = 20
MAX_GROUPS = 12
MAX_SUBSTEPS = 50_000
_NICE = [
    parse_duration(s)
    for s in ("1m", "2m", "5m", "10m", "15m", "30m", "1h", "2h", "3h", "6h", "12h", "1d")
]
_SELECTOR = re.compile(r"^\s*([A-Za-z_:][A-Za-z0-9_:]*)\s*(\{.*\})?\s*$", re.DOTALL)
_PERCENTILE_NAME = re.compile(r"(_p\d{2,3}|_pct\d+|quantile|percentile|_median)$", re.IGNORECASE)
UNIT_SECONDS = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}
ROLES = ("arrival_rate", "latency", "concurrency")

HINTS = {
    "L_high": [
        (
            "time in the system that the latency timer does not cover: queueing before the timer "
            "starts (accept queue, thread-pool or connection-pool wait, middleware) or work after "
            "it stops"
        ),
        (
            "latency measured on a subset of the requests the gauge counts (one route, successes "
            "only, sampled)"
        ),
        "a gauge counting something broader than requests (connections, keep-alive sockets)",
    ],
    "L_low": [
        "the concurrency gauge misses instances or series (compare grouped by instance/pod)",
        (
            "the gauge is sampled at scrapes that miss short bursts (raise the scrape rate or use "
            "a time-integrated in-flight counter)"
        ),
        (
            "latency measured on a superset (includes client, upstream or retry time) or the "
            "arrival counter counts more than the gauge tracks"
        ),
    ],
}
LEAK_HINT = (
    "L - lambda W grows over the windows: leaked or stuck requests (gauge incremented, never "
    "decremented) or a backlog that is building"
)
TRANSIENT_HINTS = {
    "peak": (
        "special cause at a load peak (beyond expected fluctuation, or promoted by evidence of "
        "leaving steady state): possible transition toward overload; check saturation (USE) and "
        "whether lambda approached capacity in those windows"
    ),
    "peak_common": (
        f"{PEAK_COMMON}: re-run over a longer range or compare the next peaks; a growing backlog "
        "or W rising window after window would make it a signal"
    ),
    "peak_undetermined": (
        f"{PEAK_UNDETERMINED}: re-run over a range of {MIN_SPREAD}+ windows (the process's own "
        "window-to-window variation then sets the envelope) before calling it a signal"
    ),
    "drain": "transient after a peak: a backlog draining (recovery), out of steady state",
    "other": (
        "transient not at a load peak: look for a deploy, an instance joining or leaving, or a "
        "routing / instrumentation change in those windows"
    ),
}
SOURCE_TEXT = sources.TEXT
NOT_POSSIBLE = (
    "Little's law cannot be checked without a concurrency (in-flight) signal: L must be "
    "measured, never derived from lambda x W (that would make the check circular)"
)


def _parse(role: str, text: str) -> tuple[str, str]:
    m = _SELECTOR.match(text or "")
    if not m:
        raise ValueError(
            f'{role}={text!r}: pass a metric name or a selector like name{{job="api"}}; the '
            "check writes the rate and the aggregation itself"
        )
    return m.group(1), m.group(2) or ""


def _matchers(sel: str) -> list[str]:
    return sorted(p.strip() for p in sel.strip("{}").split(",") if p.strip())


def _nice_window(span_ms: int, floor_ms: int) -> int:
    want = max(span_ms / TARGET_WINDOWS, floor_ms)
    return next((w for w in _NICE if w >= want), _NICE[-1])


def _windows(
    start: str, end: str, window: str, res: int, now: int, scrape_note: str | None = None
) -> tuple[TimeRange, int, int, int]:
    """The range on the sub-step grid, the window, the sub-step (a multiple of the
    resolution, ~SUBSTEPS_PER_WINDOW per window) and sub-steps per window.

    Windows are anchored at the requested start (rounded up to the sub-step), not at wall-clock
    multiples of the window: the range is never widened, so data before `start` (a warm-up, an
    incident the caller excluded) or after `end` is not judged, and the window grid follows the
    data. A trailing part of at least half a window is judged as a short window."""
    rng = TimeRange(parse_time(start, now), parse_time(end, now))
    span = rng.end_ms - rng.start_ms
    if span <= 0:
        raise ValueError("end must be after start")
    floor = MIN_SUBSTEPS_PER_WINDOW * res
    win = _nice_window(span, floor) if window == "auto" else parse_duration(window)
    if win < floor:
        raise ValueError(
            f"window {format_duration(win)} holds fewer than {MIN_SUBSTEPS_PER_WINDOW} scrapes "
            f"({format_duration(res)} each) (hint: window >= {format_duration(floor)})"
            + (f"; {scrape_note}" if scrape_note else "")
        )
    step = max(res, (win // SUBSTEPS_PER_WINDOW) // res * res)
    k = win // step
    win = k * step
    rng = TimeRange(-(-rng.start_ms // step) * step, rng.end_ms // step * step)
    if (rng.end_ms - rng.start_ms) // step > MAX_SUBSTEPS:
        raise ValueError(
            "range / sub-step is too many points (hint: a shorter range or longer window)"
        )
    if (rng.end_ms - rng.start_ms) < 2 * win:
        raise ValueError(
            f"the range holds fewer than two {format_duration(win)} windows (hint: a longer "
            "range or a shorter window)"
        )
    return rng, win, step, k


def _exprs(
    by: list[str], lat: dict, arr_name: str, arr_sel: str, arr_form: str, concurrency: str,
    tile: str | None = None,
) -> dict[str, str]:  # fmt: skip
    """The four queries, summed by `by`. A native histogram has no _sum/_count series: its sum
    and count are histogram_sum/histogram_count of its rate. `arr_form`: gauge (already a
    rate) | native | classic (a histogram: its _count) | counter.

    `tile` (VictoriaMetrics: the source resolution, e.g. "5s"): counters as increase(x[tile]),
    evaluated every tile inside each sub-step (the subquery step is the resolution). VM's
    increase() counts from the last sample before its window to the last one in it, without
    extrapolation, so consecutive tiles partition the counter exactly and each tile ends at the
    same scrape the gauge is read at: no lookback offset by construction. The caller divides by
    the tile to get per-second rates. Without it (Prometheus): rate(x[$__rate_interval]), whose
    window reaches back further than the gauge reading (a bias term)."""
    agg = f"sum by ({', '.join(by)})" if by else "sum"
    fn, ri = ("increase", f"[{tile}]") if tile else ("rate", "[$__rate_interval]")
    base, lsel = lat["base"], lat["selector"]
    if lat["native"]:
        sum_q = f"{agg} (histogram_sum({fn}({base}{lsel}{ri})))"
        cnt_q = f"{agg} (histogram_count({fn}({base}{lsel}{ri})))"
    else:
        sum_q = f"{agg} ({fn}({base}_sum{lsel}{ri}))"
        cnt_q = f"{agg} ({fn}({base}_count{lsel}{ri}))"
    if arr_form == "gauge":
        arr_q = f"{agg} ({arr_name}{arr_sel})"
    elif arr_form == "native":
        arr_q = f"{agg} (histogram_count({fn}({arr_name}{arr_sel}{ri})))"
    elif arr_form == "classic":
        arr_q = f"{agg} ({fn}({arr_name}_count{arr_sel}{ri}))"
    else:
        arr_q = f"{agg} ({fn}({arr_name}{arr_sel}{ri}))"
    return {
        "arrival_rate": arr_q,
        "latency_sum": sum_q,
        "latency_count": cnt_q,
        "concurrency": f"{agg} ({concurrency})",
    }


def _fill_empty_tiles(v: np.ndarray, n: np.ndarray, gauge: bool) -> tuple[np.ndarray, np.ndarray]:
    """A sub-step of one tile in which no scrape landed (jitter around the tile edge: the scrape
    before it was a little late, the one after a little early) comes back without a value: the
    source drops buckets without an observed sample. Its tile's increase is 0 — the next tile,
    which holds two scrapes, carries it — and the gauge still reads its last sample there. Left
    out, the next tile's two intervals of counts would be set against one gauge reading (lambda
    inflated: measured 1.4-1.6x with scrapes on the tile edges). Isolated gaps only (observed on
    both sides); a longer gap is missing data and stays out of all four signals."""
    gap = np.zeros(v.size, bool)
    gap[1:-1] = np.isnan(v[1:-1]) & np.isfinite(v[:-2]) & np.isfinite(v[2:])
    if not gap.any():
        return v, n
    v = v.copy()
    i = np.flatnonzero(gap)
    v[i] = v[i - 1] if gauge else 0.0
    return v, n


def _key(labels: dict, by: list[str]) -> tuple[str, ...]:
    return tuple(labels.get(b, "") for b in by)


class LittlesOps:
    def __init__(
        self,
        datasets: DatasetStore,
        query: Callable[..., Awaitable[dict]],
        resolution: Callable[[str], int],
        facts: Callable[[str, str], Any],
        catalog_has: Callable[[str, str], bool],
        catalog_any: Callable[[str], bool],
        binding: Callable[[str, str], Any],
        clock: Callable[[], int],
        family: Callable[[str, str], list[str] | None] = lambda s, m: None,
        flavor: Callable[[str], str | None] = lambda s: None,
        scrape: Callable[[str, str, int], Awaitable[int | None]] | None = None,
    ) -> None:
        self._datasets = datasets
        self._query = query
        self._resolution = resolution
        self._facts = facts
        self._has = catalog_has
        self._catalog_any = catalog_any
        self._binding = binding
        self._clock = clock
        self._family = family  # histogram family members of a catalogued base name, or None
        self._flavor = flavor  # the source's query language: victoriametrics | prometheus
        self._scrape = scrape  # a series' sample spacing (ms) around a time, or None
        self._last: dict[str, dict] = {}
        self._memo: Memo[dict] = Memo()

    # inputs -------------------------------------------------------------------------
    def _roles(
        self, source: str, binding: str | None, given: dict[str, str | None], by: list[str] | None
    ) -> tuple[dict[str, str], list[str], dict | None]:
        roles: dict[str, str | None] = {r: None for r in ROLES}
        join: list[str] = []
        bound = None
        if binding:
            b = self._binding(source, binding)  # raises when there is none
            roles.update(b.winner.roles)
            join = list(b.winner.join_on)
            bound = {"key": binding, "origin": b.winner.origin, "roles": dict(b.winner.roles)}
        roles.update({r: v for r, v in given.items() if v})
        missing = [r for r in ROLES if not roles.get(r)]
        if missing:
            hints = [SUGGESTIONS[("littles_law", r)] for r in missing]
            sug = "; ".join(
                f"{r}: e.g. {h.metric_name(binding or 'service')} ({h.type})"
                for r, h in zip(missing, hints, strict=True)
            )
            lead = (
                NOT_POSSIBLE if "concurrency" in missing
                else "Little's law needs all three signals: the check cannot be done"
            )  # fmt: skip
            raise ValueError(
                f"no signal for {', '.join(missing)}. {lead} (hint: pass it, or instrument it — "
                f"{sug})"
            )
        return {r: str(roles[r]) for r in ROLES}, list(by) if by is not None else join, bound

    def _latency(self, source: str, text: str) -> dict:
        refusal = (
            f"latency={text!r} looks like a percentile: Little's law needs the MEAN latency "
            "W = rate(_sum) / rate(_count); averaging or reusing a percentile is not a mean "
            "(hint: bind the histogram or summary that has _sum and _count, e.g. "
            "<service>_request_duration_seconds; with percentiles only, add a histogram)"
        )
        if "quantile" in text:
            raise ValueError(refusal)
        name, sel = _parse("latency", text)
        if _PERCENTILE_NAME.search(name):
            raise ValueError(refusal)
        base = re.sub(r"_(bucket|sum|count)$", "", name)
        facts = self._facts(source, name)
        if getattr(facts, "statistic", None) == "quantile":
            raise ValueError(refusal)
        return {
            "base": base,
            "selector": sel,
            "native": self.native_histogram(source, base, refusal),
        }

    def native_histogram(self, source: str, base: str, refusal: str | None = None) -> bool:
        """True for a native histogram (no _bucket/_sum/_count series). With a catalog, a base
        name without _sum/_count there is refused when `refusal` is given (percentile-only)."""
        members = self._family(source, base)
        if members:
            return native_family(members)
        if not self._catalog_any(source):
            return False  # nothing known: classic; the queries come back empty if wrong
        if self._has(source, f"{base}_sum") and self._has(source, f"{base}_count"):
            return False
        if self._has(source, base) and getattr(self._facts(source, base), "type", None) == (
            "histogram"
        ):
            return True
        if refusal is None:
            return False
        raise ValueError(
            f"no {base}_sum / {base}_count on {source!r}: " + refusal.split(": ", 1)[1]
        )

    def _unit(self, source: str, base: str, given: str | None) -> tuple[float, str, str | None]:
        if given:
            u = given.strip().lower()
            if u not in UNIT_SECONDS:
                raise ValueError(f"latency_unit must be one of {', '.join(UNIT_SECONDS)}")
            return UNIT_SECONDS[u], u, "given"
        facts = self._facts(source, base)
        if getattr(facts, "unit", None) not in UNIT_SECONDS:
            facts = self._facts(source, f"{base}_sum")
        u = getattr(facts, "unit", None)
        if u in UNIT_SECONDS:
            return UNIT_SECONDS[u], u, getattr(facts, "unit_provenance", None) or "catalog"
        if u not in (None, "count"):
            raise ValueError(
                f"{base} has unit {u!r}, not a time: Little's law needs latency in a time unit "
                "(hint: pass latency_unit if the catalog is wrong)"
            )
        return 1.0, "s", None  # assumed: flagged

    async def _probe_scrape(
        self, source: str, selector: str, res: int, start: str, end: str
    ) -> tuple[int, str | None]:
        """The gauge's scrape interval (median sample spacing at the range end) against the
        source's resolution (learned from scrape spacing or configured): the sub-step grid, the
        gauge's sample count and the counters' tiles follow the source's, so a mismatch is
        stated (and a refused window says how to change it). Returns (the spacing used for the error terms: the larger,
        a note or None)."""
        if self._scrape is None:
            return res, None
        try:
            at = parse_time(end, self._clock())
            got = await self._scrape(source, selector, at)
        except Exception:  # noqa: BLE001 - a probe failure must not stop the check
            return res, None
        if not got or abs(got - res) <= res // 5:
            return res, None
        note = (
            f"{selector.split('{')[0]} is scraped every {format_duration(got)} (median sample "
            f"spacing at the range end) but source {source!r} is read at resolution "
            f"{format_duration(res)}"
        )
        if got < res:
            note += (
                f": the check reads one sample per {format_duration(res)} (hint: "
                f"source_status({source!r}) shows where the resolution comes from; "
                f"source_learn(source={source!r}) re-measures the scrape spacing, or "
                f'source_connect(name={source!r}, url=..., resolution="{format_duration(got)}", '
                "replace=true) sets it, to use them all)"
            )
        else:
            note += ": samples are re-read between scrapes; the error terms use the scrape spacing"
        return max(got, res), note

    # fetch + run ------------------------------------------------------------------------
    async def check(
        self,
        *,
        source: str = "default",
        binding: str | None = None,
        arrival_rate: str | None = None,
        latency: str | None = None,
        concurrency: str | None = None,
        by: list[str] | None = None,
        start: str = "now-6h",
        end: str = "now",
        window: str = "auto",
        warmup: str | None = None,
        latency_unit: str | None = None,
        arrivals: str = "auto",
        actor: str = "claude",
    ) -> dict:
        if arrivals not in ("auto", "arrivals", "completions"):
            raise ValueError("arrivals is auto, arrivals or completions")
        roles, by, bound = self._roles(
            source, binding,
            {"arrival_rate": arrival_rate, "latency": latency, "concurrency": concurrency}, by,
        )  # fmt: skip
        for b in by:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", b):
                raise ValueError(f"by: {b!r} is not a label name")
        lat = self._latency(source, roles["latency"])
        arr_name, arr_sel = _parse("arrival_rate", roles["arrival_rate"])
        conc_name, conc_sel = _parse("concurrency", roles["concurrency"])
        arr_type = getattr(self._facts(source, arr_name), "type", None)
        if getattr(self._facts(source, conc_name), "type", None) == "counter":
            raise ValueError(
                f"concurrency={conc_name!r} is a counter: L is the number in flight, a gauge "
                "(hint: bind an in-flight / active-requests gauge)"
            )
        factor, unit, unit_basis = self._unit(source, lat["base"], latency_unit)
        res = self._resolution(source)
        scrape_ms, scrape_note = await self._probe_scrape(
            source, f"{conc_name}{conc_sel}", res, start, end
        )
        rng, win, step, k = _windows(start, end, window, res, self._clock(), scrape_note)
        skip = 1 + (-(-parse_duration(warmup) // step) if warmup else 0)
        arr_is_rate = arr_type == "gauge"
        # a histogram named as the arrival signal: its count (classic _count, native
        # histogram_count) counts completions
        arr_hist = arr_type == "histogram" or arr_name == lat["base"]
        arr_native = arr_hist and self.native_histogram(source, arr_name)
        if arr_is_rate:
            arr_form = "gauge"
        elif arr_native:
            arr_form = "native"
        elif arr_hist:
            arr_form = "classic"
        else:
            arr_form = "counter"
        tiled = self._flavor(source) == "victoriametrics"
        exprs = _exprs(
            by, lat, arr_name, arr_sel, arr_form, f"{conc_name}{conc_sel}",
            format_duration(res) if tiled else None,
        )  # fmt: skip
        ds = {}
        for role, expr in exprs.items():
            out = await self._query(
                expr, start=str(rng.start_ms), end=str(rng.end_ms),
                step=format_duration(step), source=source, actor=actor,
            )  # fmt: skip
            ds[role] = out["dataset"]
        if arrivals == "auto":
            same = arr_name == f"{lat['base']}_count" or arr_name == lat["base"]
            arrivals = "completions" if same else "unknown"
        cfg = {
            "source": source, "datasets": ds, "by": by, "step_ms": step, "window_ms": win,
            "k": int(k), "skip": int(skip), "start_ms": rng.start_ms, "end_ms": rng.end_ms,
            "unit_factor": factor, "unit": unit, "unit_basis": unit_basis,
            # how much further back the counters' window reaches than the gauge reading it is
            # compared with: 0 with increase() tiles (VM); rate(x[ri]) at t covers (t - ri, t]
            # (VM also takes the sample before it), the gauge is read at the last scrape <= t,
            # on average half a scrape back: (ri - scrape) / 2 (measured on VM v1.137: ri / 2
            # behind the bucket's middle at sub-steps of 5s-60s)
            "lookback_ms": 0 if tiled else max(0, (rate_interval_ms(step, res) - res) // 2),
            "counter_form": "increase_tiles" if tiled else "rate_interval",
            "tile_s": res / 1000 if tiled else None,
            "scrape_ms": scrape_ms, "scrape_probe": scrape_note,
            "resolution_ms": res, "arrivals": arrivals, "arrival_is_rate": arr_is_rate,
            "roles": roles, "binding": bound, "exprs": exprs, "warmup": warmup,
            "matchers": {
                "arrival_rate": _matchers(arr_sel), "latency": _matchers(lat["selector"]),
                "concurrency": _matchers(conc_sel),
            },
        }  # fmt: skip
        self._last[ds["concurrency"]] = cfg
        return self.summary(cfg)

    def last_config(self, dataset_id: str) -> dict:
        cfg = self._last.get(dataset_id)
        if cfg is None:
            raise ValueError(
                f"no Little's law check on {dataset_id} yet (hint: check_littles_law first; show "
                "the concurrency dataset it returns)"
            )
        return cfg

    def _grouped(self, dataset_id: str, by: list[str], grid: np.ndarray, step: int) -> tuple:
        """Per group key: (values on the grid, sample counts, labels); labels missing a `by`."""
        _, result = self._datasets.get(dataset_id)
        labels = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
        df = pl.from_arrow(result.buckets)
        assert isinstance(df, pl.DataFrame)
        out: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        names: dict[tuple, dict] = {}
        lacking: set[str] = set()
        for (sid,), g in df.group_by("series_id", maintain_order=True):
            lb = labels.get(str(sid), {})
            lacking |= {b for b in by if b not in lb}
            key = _key(lb, by)
            idx = (g["ts_ms"].to_numpy() - grid[0]) // step
            keep = (idx >= 0) & (idx < grid.size)
            v = np.full(grid.size, np.nan)
            n = np.zeros(grid.size)
            avg = g["avg"].to_numpy().astype(float)
            v[idx[keep]] = avg[keep]
            n[idx[keep]] = g["count"].to_numpy()[keep]
            if key in out:  # two series fold into one key (a by label missing): add them up
                pv, pn = out[key]
                v = np.where(np.isnan(pv), v, np.where(np.isnan(v), pv, pv + v))
                n = np.maximum(pn, n)
            out[key] = (v, n)
            names[key] = {b: lb.get(b, "") for b in by}
        return out, names, sorted(lacking)

    def run(self, cfg: dict) -> dict:
        key = json.dumps(cfg, sort_keys=True, default=str)
        if (hit := self._memo.get(key)) is not None:
            return hit
        step, by = cfg["step_ms"], cfg["by"]
        grid = np.arange(cfg["start_ms"], cfg["end_ms"] + 1, step, dtype=np.int64)
        per_role, names, lacking = {}, {}, {}
        for role, did in cfg["datasets"].items():
            per_role[role], nm, lacking[role] = self._grouped(did, by, grid, step)
            names.update(nm)
        keys = sorted(set().union(*(set(v) for v in per_role.values())))
        nan = np.full(grid.size, np.nan)

        if cfg.get("tile_s"):
            for role, groups in per_role.items():
                for k, (v, n) in groups.items():
                    groups[k] = _fill_empty_tiles(v, n, gauge=role == "concurrency")
        # increase() tiles: per-tile counts -> per second
        per_s = 1 / cfg["tile_s"] if cfg.get("tile_s") else 1.0
        arr_s = 1.0 if cfg["arrival_is_rate"] else per_s

        def sub(k: tuple) -> Substeps:
            get = lambda role: per_role[role].get(k, (nan, np.zeros(grid.size)))
            conc, conc_n = get("concurrency")
            return Substeps(
                grid, get("arrival_rate")[0] * arr_s,
                get("latency_sum")[0] * cfg["unit_factor"] * per_s,
                get("latency_count")[0] * per_s, conc, conc_n, step, cfg["lookback_ms"],
                cfg.get("scrape_ms", cfg["resolution_ms"]),
            )  # fmt: skip

        matched, unmatched = [], []
        for k in keys:
            have = [r for r in per_role if k in per_role[r]]
            if len(have) == len(per_role):
                matched.append(k)
            else:
                unmatched.append({
                    "labels": names.get(k, {}),
                    "present_in": sorted({_role(r) for r in have}),
                    "missing_in": sorted({_role(r) for r in per_role if r not in have}),
                })  # fmt: skip
        subs = {k: sub(k) for k in keys}
        n_win = max(1, (grid.size - cfg["skip"]) // cfg["k"])
        grouped = bool(by) and len(keys) > 1
        tests = n_win * ((len(matched) if grouped else 0) + 1)
        arr = cfg["arrivals"]
        total = check(
            combine(list(subs.values())) if keys else sub(()), cfg["k"], cfg["skip"], tests,
            arrivals=arr,
        )  # fmt: skip
        groups = (
            [
                (names[k], check(subs[k], cfg["k"], cfg["skip"], tests, arrivals=arr))
                for k in matched
            ]
            if grouped
            else []
        )
        out = {
            "total": total, "groups": groups, "unmatched": unmatched, "lacking": lacking,
            "no_concurrency": not per_role["concurrency"],
        }  # fmt: skip
        self._memo.put(key, out)
        return out

    # wire -----------------------------------------------------------------------------
    def _assumptions(self, cfg: dict, r: dict) -> list[dict]:
        tot: GroupResult = r["total"]
        allw = [w for _, g in [("total", tot), *r["groups"]] for w in g.windows]
        flagged = lambda f: [_span(w) for w in allw if f in w.flags]
        out = []
        ns = flagged("not_steady")
        out.append({
            "name": "steady_state", "status": "flagged" if ns else "ok",
            "detail": (
                "trend test per window on L and lambda (n_eff-adjusted, 1%): "
                + (f"material drift (> 10%) in {len(ns)} window(s); Little's law still holds over "
                   "a window up to the edge term, which the interval carries" if ns
                   else "no material drift")
            ),
            **({"windows": ns[:10]} if ns else {}),
        })  # fmt: skip
        fi = flagged("flow_imbalance")
        what = {
            "completions": "lambda counts completions (the counter is the latency histogram's "
            "_count)",
            "arrivals": "lambda counts arrivals (stated by the caller)",
            "unknown": "lambda is the counter's rate; whether it counts arrivals or completions is "
            "unknown (most middleware increments at completion); in steady state they are equal",
        }[cfg["arrivals"]]
        out.append({
            "name": "arrivals_vs_completions", "status": "flagged" if fi else "assumed",
            "detail": what + (
                f"; the counter and the latency count disagree beyond the backlog change in "
                f"{len(fi)} window(s): latency measured on a subset/superset of the counted "
                "requests, or requests dropped" if fi else
                "; counter rate matches the latency count rate within each window"
            ),
            **({"windows": fi[:10]} if fi else {}),
        })  # fmt: skip
        m = cfg["matchers"]
        differ = len({tuple(v) for v in m.values()}) > 1
        lacking = {k: v for k, v in r["lacking"].items() if v}
        issues = []
        if r["unmatched"]:
            issues.append(f"{len(r['unmatched'])} group(s) missing from some signals")
        if lacking:
            issues.append(
                "by label(s) absent on " + ", ".join(f"{k}: {v}" for k, v in lacking.items())
            )
        if differ:
            issues.append(
                "selectors differ across roles: "
                + "; ".join(f"{k} {{{', '.join(v)}}}" for k, v in m.items())
            )
        out.append({
            "name": "label_sets", "status": "flagged" if issues else "ok",
            "detail": (
                f"aggregated {'by ' + ', '.join(cfg['by']) if cfg['by'] else 'to one total'}; "
                + ("; ".join(issues) if issues else "the same groups in all three signals and the "
                   "same selectors")
            ),
        })  # fmt: skip
        out.append({
            "name": "units", "status": "ok" if cfg["unit_basis"] else "assumed",
            "detail": (
                f"latency in {cfg['unit']} ({cfg['unit_basis'] or 'no unit known: ASSUMED seconds'})"
                ", converted to seconds; lambda per second"
                + (" (the arrival signal is a gauge, used as a rate)" if cfg["arrival_is_rate"] else
                   " from the counter's increase")
                + "; L in requests"
            ),
        })  # fmt: skip
        tiles = cfg.get("counter_form") == "increase_tiles"
        out.append({
            "name": "window_alignment", "status": "ok",
            "detail": (
                f"all four signals on one {format_duration(cfg['step_ms'])} grid, windows anchored "
                "at the requested start (the range is not widened); sub-steps missing any signal "
                "are dropped from all; "
                + (
                    f"counters as increase() over {format_duration(cfg['resolution_ms'])} tiles "
                    "that partition them exactly and end at the scrape the gauge is read at: no "
                    "lookback" if tiles else
                    "rate() looks back "
                    f"{format_duration(cfg['lookback_ms']) if cfg['lookback_ms'] else '0s'} further "
                    "than the gauge reading: a bias bound in the interval"
                )
                + "; the gauge's end-of-interval reading is corrected (trapezoid); a load episode "
                "that starts and ends inside one window balances over it (Little's law holds over "
                "that window): use windows shorter than the episodes to see them"
            ),
        })  # fmt: skip
        out.append({
            "name": "warmup", "status": "ok" if cfg["warmup"] else "assumed",
            "detail": (
                f"first {cfg['warmup']} excluded" if cfg["warmup"]
                else "nothing excluded: if the service started in the range, pass warmup"
            ),
        })  # fmt: skip
        fl = [w for w in allw if w.sd_source == "floor"]
        probe = cfg.get("scrape_probe")
        out.append({
            "name": "gauge_sampling", "status": "flagged" if fl or probe else "ok",
            "detail": (
                f"L is the gauge's average over scrapes every {format_duration(cfg['resolution_ms'])}"
                "; short spikes between scrapes are invisible, so the interval is never narrower "
                "than a Poisson-occupancy process would need"
                + (f" (that floor set the width in {len(fl)} window(s))" if fl else "")
                + (f"; {probe}" if probe else "")
            ),
        })  # fmt: skip
        sh = flagged("window_short_vs_latency")
        if sh:
            out.append({
                "name": "window_vs_latency", "status": "flagged",
                "detail": f"W > window/10 in {len(sh)} window(s): edge effects are large (longer window)",
            })  # fmt: skip
        return out

    def summary(self, cfg: dict) -> dict:
        r = self.run(cfg)
        tot: GroupResult = r["total"]
        L_ds = cfg["datasets"]["concurrency"]
        groups = [_group(cfg, L_ds, {}, tot)]
        groups += [_group(cfg, L_ds, lb, g) for lb, g in r["groups"]]
        hints: list[str] = []
        directions = {g["verdict"] for g in groups} | {
            w["verdict"] for g in groups for w in g["flagged_windows"]
        }
        for v in ("L_high", "L_low"):
            if v in directions:
                hints += HINTS[v]
        phases = {
            "peak" if t["phase"] == "peak" and t["source"] == SPECIAL
            else "peak_undetermined" if t["phase"] == "peak" and t["source"] == UNDETERMINED
            else "peak_common" if t["phase"] == "peak" else t["phase"]
            for g in groups for t in g["classification"]["transient"]
        }  # fmt: skip
        if any(g["classification"]["promoted"] for g in groups):
            phases.add("peak")
        hints += [
            TRANSIENT_HINTS[ph]
            for ph in ("peak", "peak_undetermined", "peak_common", "drain", "other")
            if ph in phases
        ]
        if any((g.get("growing") or {}).get("growing") for g in groups):
            hints.insert(0, LEAK_HINT)
        ratio = tot.pooled.ratio
        if ratio and (500 < ratio < 2000 or 5e-4 < ratio < 2e-3):
            hints.insert(
                0, "the ratio is ~1000x off: check the latency unit (ms vs s; latency_unit)"
            )
        if r["unmatched"]:
            hints.insert(0, "groups missing from some signals: " + "; ".join(
                f"{_labels_text(u['labels'])} missing in {', '.join(u['missing_in'])}" for u in r["unmatched"][:5]
            ))  # fmt: skip
        if r["no_concurrency"]:
            sug = SUGGESTIONS[("littles_law", "concurrency")]
            hints.insert(0, (
                f"{NOT_POSSIBLE}: the concurrency query returned no data (hint: instrument an "
                f"in-flight gauge, e.g. {sug.metric_name(cfg['binding']['key'] if cfg['binding'] else 'service')} "
                f"({sug.type}); {sug.why})"
            ))  # fmt: skip
        if not cfg["by"] and tot.verdict != "consistent":
            hints.append(
                "to localise it to series, re-run with by=[a label all three share, e.g. instance]"
            )
        caveats = []
        if not cfg["unit_basis"]:
            caveats.append("latency_unit_assumed")
        warnings = _warnings(groups, r["no_concurrency"])
        out = {
            "summary": _summary_text(cfg, groups[0], warnings, r["no_concurrency"]),
            "discrepancy": groups[0]["discrepancy"],
            "verdict": tot.verdict,
            "classification": groups[0]["classification"],
            "warnings": warnings,
            "variation": _variation(groups[0], caveats),
            "question": "Is mean concurrency L consistent with throughput x mean latency (L = lambda W)?",
            "range": [iso(cfg["start_ms"]), iso(cfg["end_ms"])],
            "window": format_duration(cfg["window_ms"]),
            "substep": format_duration(cfg["step_ms"]),
            "datasets": cfg["datasets"],
            "binding": cfg["binding"],
            "total": groups[0],
            **({"groups": groups[1:]} if len(groups) > 1 else {}),
            **({"unmatched": r["unmatched"]} if r["unmatched"] else {}),
            "assumptions": self._assumptions(cfg, r),
            "method": METHOD,
            "hints": hints,
            "caveats": caveats,
            "draw": f'show("{L_ds}", question, mark="littles")',
        }
        # the interval is built from the four series' own sampling and timing error; any
        # declared (or unknown) uncertainty of those datasets is not folded in (spec §5.3)
        return mark_statistics(out, self._datasets, list(cfg["datasets"].values()))

    def panel(self, cfg: dict) -> dict:
        r = self.run(cfg)
        groups = [("total", {}, r["total"])] + [
            (_labels_text(lb), lb, g) for lb, g in r["groups"][:MAX_GROUPS]
        ]
        return {
            "kind": "littles",
            "effective_step_ms": cfg["window_ms"],
            "window_ms": cfg["window_ms"],
            "substep_ms": cfg["step_ms"],
            "series": [
                {
                    "id": gid, "labels": lb, "verdict": g.verdict,
                    "pooled": _block_wire(g.pooled),
                    "windows": [
                        {**_block_wire(w), "reference": sig(g.references.get(i))}
                        for i, w in enumerate(g.windows)
                    ],
                    "reference": sig(g.reference),
                    "systematic": _systematic_wire(g),
                    "transient": [
                        {"index": t["index"], "phase": t["phase"], "source": t["source"],
                         "promoted": t["promoted"], **_panel_span(t)}
                        for t in g.transient
                    ],
                    "promoted": [
                        {"index": p["index"], "from": p["from"], "deviation": p["deviation"],
                         "reason": p["reason"],
                         "evidence": sorted({e["kind"] for e in p["evidence"] if e["significant"]}),
                         **_panel_span(p)}
                        for p in g.promoted
                    ],
                    "common_cause": _common_wire(g),
                }
                for gid, lb, g in groups
            ],
            "unmatched": r["unmatched"],
            "more_groups": max(0, len(r["groups"]) - MAX_GROUPS),
            "caveats": [] if cfg["unit_basis"] else ["latency_unit_assumed"],
        }  # fmt: skip


METHOD = (
    "R = L / (lambda W): L the gauge's time average (its end-of-interval readings corrected to "
    "the trapezoid), lambda the counter's rate, W = Δ_sum / Δ_count (a mean, never a percentile); "
    "on VictoriaMetrics the counters are read as increase() over scrape-interval tiles that "
    "partition them exactly and end at the gauge's scrapes (no lookback), elsewhere as "
    "rate(x[$__rate_interval]); windows are anchored at the requested start. The discrepancy "
    "L - lambda W and "
    "R - 1 are always reported. Measurement interval (what the instruments add for this window; "
    "source: measurement system): gauge sampling (successive differences of the scrapes, floored "
    "by a Poisson-occupancy process sampled the same way), the steady-state straddle of requests "
    "in flight at the window edges, counter scrape timing (increments between an edge and the "
    "nearest scrape; lambda and W linearly), in quadrature on log R, plus a bias bound for the "
    "rate window's lookback (rate() only); t with n-1 df. The counts' Poisson noise is not in it: over a window "
    "L and lambda W describe the same requests; it is the common-cause scale (a small system's "
    "per-window fluctuation), reported apart. Systematic offset (measurement system: "
    "instrumentation / model mismatch): the level of the windows that are not transient, equal "
    "weights, its error the larger of measurement and window spread, at 2.5%. Transient windows: "
    "off that level (or 1) beyond the measurement interval, Bonferroni over windows and groups "
    "at 2.5%; special cause when also beyond the common-cause envelope (small-system scale or "
    "3 robust sigma of the windows' own variation), else common cause. With fewer than 5 "
    "windows (no estimate of their own variation) the label rests on a cautious envelope: the "
    "small-system terms inflated by the window's own sub-steps (sqrt of the arrivals' long-run "
    "variance-to-mean ratio; the effective latency CV from latency-seconds - W x completions), "
    "around the window's linear trend, x autocorrelation time, never below Poisson; beyond the "
    "Poisson envelope only: undetermined, both shown. 5% false alarms overall "
    "at most. A window at a load peak inside the envelope (or the measurement interval) is "
    "PROMOTED to special cause only on independent evidence of leaving steady state, with its "
    f"own {100 * PROMOTE_ALPHA:.0f}% family-wise budget, a third each: (a) the backlog growing "
    "(gauge N(end) - N(start); when the counter counts arrivals also arrivals - completions, and "
    "the smaller of the two) beyond Cantelli's distribution-free bound on sqrt(2 var N), var N from the gauge "
    "outside the peak's episode, Bonferroni over windows and groups; (b) W rising across two "
    "consecutive windows into or through the peak, each rise beyond a t threshold on the "
    "windows' standard errors around their own trend (n_eff df), Bonferroni over windows, "
    "groups and the two triples; (c) the peak deviation growing across 3+ load episodes, "
    "Kendall's S exact one-sided p, Bonferroni over groups."
)


def _warnings(groups: list[dict], no_concurrency: bool) -> list[str]:
    out: list[str] = []
    if no_concurrency:
        out.append(NOT_POSSIBLE + ": the concurrency signal returned no data")
    span = lambda w: f"{w[0]}–{w[1]}"
    for g in groups:
        name = _labels_text(g["labels"] or {})
        c = g["classification"]
        peaks = [t for t in c["transient"] if t["at_peak"] and not t["promoted"]]
        special = [t for t in peaks if t["source"] == SPECIAL]
        undecided = [t for t in peaks if t["source"] == UNDETERMINED]
        common = [t for t in peaks if t["source"] not in (SPECIAL, UNDETERMINED)]
        if special:
            out.append(
                f"{name}: transient at a load peak in "
                + ", ".join(span(t["window"]) for t in special[:4])
                + ", beyond expected fluctuation (special cause): possible transition out of "
                "steady state (toward overload)"
            )
        for p in c["promoted"][:4]:
            out.append(
                f"{name}: {span(p['window'])} at a load peak, leaving steady state (special "
                f"cause, promoted from {sources.text(p['from'])}): {p['reason']}"
            )
        if undecided:
            out.append(
                f"{name}: {', '.join(span(t['window']) for t in undecided[:4])} {PEAK_UNDETERMINED}"
            )
        if common:
            out.append(f"{name}: {', '.join(span(t['window']) for t in common[:4])} {PEAK_COMMON}")
        w = g["common_cause"].get("warning")
        if w:
            out.append(f"{name}: {w}")
    return out


def _n(v: float | None) -> str:
    return "–" if v is None else f"{v:.3g}"


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:+.0f}%"


def _special_lead(g: dict) -> str | None:
    """The special-cause windows (transient beyond the envelope, promoted load peaks) as the
    summary's opening sentence: the verdict answers only whether L = λW holds over the range, so
    "consistent" with a load peak promoted is not "nothing happened" (vayr, option C of 83w)."""
    c = g["classification"]
    special = [
        (t["window"][0], "at a load peak" if t.get("at_peak") or t["phase"] == "peak"
         else "draining a backlog" if t["phase"] == "drain" else "transient")
        for t in c["transient"] if t["source"] == SPECIAL
    ] + [(p["window"][0], "at a load peak, promoted") for p in c["promoted"]]  # fmt: skip
    if not special:
        return None
    special = sorted(dict(special).items())
    items = "; ".join(f"{w} ({what})" for w, what in special[:6])
    more = f" (+{len(special) - 6} more)" if len(special) > 6 else ""
    return (
        f"Special cause in {len(special)} window(s): {items}{more}. The verdict below answers only "
        f"whether L = λW holds over the range; these windows are where the system left steady "
        f"state."
    )


def _summary_text(cfg: dict, g: dict, warnings: list[str], no_concurrency: bool) -> str:
    """Special-cause windows first when there are any, then the discrepancy, the verdict and its
    classification, then the warnings."""
    if no_concurrency:
        return NOT_POSSIBLE + " (the concurrency query returned no data); L was not estimated."
    d = g["discrepancy"]
    if d["ratio"] is None:
        return f"Not judged: {g.get('reason') or g['verdict']}."
    win = format_duration(cfg["window_ms"])
    pw = d["per_window"]
    lead = _special_lead(g)
    parts = ([lead] if lead else []) + [
        (
            f"Discrepancy over the range: L − λ·W = {d['difference']:+.3g} requests (L "
            f"{_n(d['L'])} vs λ·W {_n(d['lambda_W'])}; L ÷ λW {_n(d['ratio'])}, "
            f"{_pct(d['relative'])}, measurement interval {_pct(d['relative_ci95'][0])} to "
            f"{_pct(d['relative_ci95'][1])})."
        )
    ]
    if pw["windows"]:
        parts.append(
            f"Per {win} window: L ÷ λW {_n(pw['ratio_range'][0])}–{_n(pw['ratio_range'][1])} "
            f"over {pw['windows']} judged window(s)."
        )
    c = g["classification"]
    sysd = c["systematic"]
    verdict = f"Verdict {g['verdict']}"
    if sysd:
        drift = " and drifting (L − λW trends over the range)" if sysd["drifting"] else ""
        parts.append(
            f"{verdict}: systematic offset L ÷ λW {_n(sysd['ratio'])} "
            f"[{_n(sysd['ci95'][0])}, {_n(sysd['ci95'][1])}] in {sysd['windows'][0]} of "
            f"{sysd['windows'][1]} windows{drift} — source: measurement system "
            "(instrumentation / model mismatch, not the process)."
        )
    else:
        parts.append(f"{verdict}: no systematic offset detected.")
    tr = c["transient"]
    if tr:
        items = "; ".join(
            f"{t['window'][0]}: L ÷ λW {_n(t['ratio'])} vs reference {_n(t['reference'])} "
            f"({SOURCE_TEXT[t['source']]}"
            + (" — promoted" if t["promoted"] else "")
            + f", {t['phase']}"
            + (", AT A LOAD PEAK" if t["at_peak"] else "")
            + ")"
            for t in tr[:6]
        )
        more = f" (+{len(tr) - 6} more)" if len(tr) > 6 else ""
        parts.append(f"Transient windows ({len(tr)}): {items}{more}.")
    else:
        parts.append("Transient windows: none.")
    pr = c["promoted"]
    if pr:
        items = "; ".join(
            f"{p['window'][0]} (L ÷ λW {_n(p['ratio'])}, {p['deviation'].replace('_', ' ')}): "
            f"{p['reason']}"
            for p in pr[:4]
        )
        parts.append(
            f"Leaving steady state at a load peak — promoted to special cause ({len(pr)}): {items}."
        )
    cc = g["common_cause"]
    parts.append(
        f"Common cause: at this traffic (N≈{cc['completions_per_window']:.0f} completions per "
        f"window) L and λW fluctuate ±{100 * (cc['rel95'] or 0):.0f}% per window"
        + (
            f"; the windows' own variation spans ±{100 * cc['spread_rel']:.0f}%"
            if cc.get("spread_rel") else ""
        ) + (
            f"; with {pw['windows']} window(s) (< {MIN_SPREAD}) their own variation cannot be "
            f"estimated, so special cause rests on the cautious envelope ±"
            f"{100 * cc['cautious_rel95']:.0f}% (the windows' own arrival bursts and latency "
            "variation); beyond the Poisson one only: undetermined"
            if cc.get("cautious_rel95") is not None else ""
        ) + "."
    )  # fmt: skip
    if warnings:
        parts.append("Warnings: " + " | ".join(warnings))
    return " ".join(parts)


def _role(r: str) -> str:
    return "latency" if r.startswith("latency") else r


def _labels_text(lb: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in lb.items()) or "total"


def _span(w: Block) -> list[str]:
    return [iso(w.start_ms), iso(w.end_ms)]


def _block_wire(b: Block) -> dict:
    d = asdict(b)
    out: dict[str, Any] = {
        "start_ms": b.start_ms,
        "end_ms": b.end_ms,
        "verdict": b.verdict,
        "n": b.n,
    }
    for k in ("L", "lam", "W_s", "lambda_W", "ratio", "sd", "diff", "common"):
        out[k] = sig(d[k])
    for k in ("L_ci", "lambda_W_ci", "ci95", "ci_test", "diff_ci"):
        out[k] = sig_pair(d[k]) if d[k] is not None else None
    out["source"] = b.source
    out["flags"] = list(b.flags)
    if b.reason:
        out["reason"] = b.reason
    return out


def _rel(ci: tuple[float, float] | None) -> list[float | None] | None:
    return sig_pair((ci[0] - 1, ci[1] - 1)) if ci else None


def _systematic_wire(g: GroupResult) -> dict | None:
    s = g.systematic
    if s is None or s.ratio is None:
        return None
    judged = sum(w.ratio is not None for w in g.windows)
    return {
        "direction": s.verdict, "ratio": sig(s.ratio), "ci95": sig_pair(s.ci95) if s.ci95 else None,
        "relative": sig(s.ratio - 1), "relative_ci95": _rel(s.ci95),
        "difference": sig(s.diff), "difference_ci95": sig_pair(s.diff_ci) if s.diff_ci else None,
        "windows": [len(g.core), judged],
        "drifting": bool(g.growing and g.growing["significant"]),
        "source": MEASUREMENT,
        "meaning": (
            "a persistent L != lambda W across most windows: the instruments do not describe the "
            "same requests (unmeasured queueing, a missing instance, units, latency on a subset "
            "or superset) — the measurement system, not the process"
        ),
    }  # fmt: skip


def _common_wire(g: GroupResult) -> dict:
    c = g.common_cause
    return {
        "completions_per_window": sig(c.get("completions_per_window")),
        "rel95": sig(c.get("rel95")),
        "pooled_rel95": sig(c.get("pooled_rel95")),
        "spread_rel": sig(c.get("spread_rel")),
        "envelope": c.get("envelope"),
        **({"cautious_rel95": sig(c["cautious_rel95"])} if "cautious_rel95" in c else {}),
        "warning": c.get("warning"),
        "source": COMMON,
    }


def _panel_span(t: dict) -> dict:
    """A shifted-grid entry has no main-grid window: the panel gets its own span."""
    if not t.get("grid"):
        return {}
    w = t["block"]
    return {"grid": "offset", "start_ms": w.start_ms, "end_ms": w.end_ms}


def _ref(g: GroupResult, t: dict) -> float:
    """The reference a transient / promoted window was judged against (its own grid's)."""
    if t.get("grid"):
        return t["reference"]
    return g.references.get(t["index"], g.reference)


def _grid(t: dict) -> dict:
    """Entries found on the grid shifted by half a window say so (their own span)."""
    return {"grid": "offset"} if t.get("grid") else {}


def _transient_wire(g: GroupResult, t: dict) -> dict:
    w = window_of(g, t)
    ref = _ref(g, t)
    return {
        "window": _span(w), "direction": t["direction"], "ratio": sig(w.ratio),
        "ci95": sig_pair(w.ci95) if w.ci95 else None, "relative": sig((w.ratio or 0) - 1),
        "difference": sig(w.diff), "difference_ci95": sig_pair(w.diff_ci) if w.diff_ci else None,
        "reference": sig(ref), "vs_reference": sig(t["vs_reference"]),
        "common_cause_rel95": sig(w.common), "source": t["source"], "phase": t["phase"],
        **({"envelope": _envelope_wire(w, t["envelope"])} if t.get("envelope") else {}),
        "at_peak": t["at_peak"],
        "load": {k: (sig(v) if isinstance(v, float) else v) for k, v in t["load"].items()},
        "cause": t["cause"], "promoted": t["promoted"],
        **({"promotion": _promotion_wire(t["promotion"])} if t["promoted"] else {}),
        **_grid(t),
    }  # fmt: skip


def _envelope_wire(w: Block, e: dict) -> dict:
    """The deviation against each envelope (principle 16: the label rests on `label_rests_on`)."""
    out = {
        "deviation": sig(e["deviation"]),
        "small_system": {"rel95": sig(e["small_system"]),
                         "assumes": "Poisson arrivals and completions, latency CV 1"},
        "label_rests_on": e["label_rests_on"],
    }  # fmt: skip
    if e.get("spread") is not None:
        out["spread"] = {"rel95": sig(e["spread"]),
                         "assumes": "3 robust sigma of the windows' own variation"}  # fmt: skip
    if e.get("cautious") is not None:
        out["cautious"] = {
            "rel95": sig(e["cautious"]),
            "k_arrivals": sig(w.common_terms.get("k_arrivals")),
            "k_latency": sig(w.common_terms.get("k_latency")),
            "assumes": "the small-system terms inflated by the window's own sub-step arrival "
            "dispersion and effective latency CV (around its linear trend; never below Poisson)",
        }
    return out


def _evidence_wire(e: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in e.items():
        if isinstance(v, float):
            out[k] = sig(v)
        elif isinstance(v, tuple):
            out[k] = sig_pair(v)
        elif isinstance(v, list):
            out[k] = [sig(x) if isinstance(x, float) else x for x in v]
        else:
            out[k] = v
    return out


def _promotion_wire(p: dict) -> dict:
    return {
        "from": p["from"], "reason": p["reason"],
        "evidence": [_evidence_wire(e) for e in p["evidence"]],
    }  # fmt: skip


def _promoted_wire(g: GroupResult, p: dict) -> dict:
    w = window_of(g, p)
    return {
        **_grid(p),
        "window": _span(w), "ratio": sig(w.ratio), "ci95": sig_pair(w.ci95) if w.ci95 else None,
        "relative": sig((w.ratio or 0) - 1), "difference": sig(w.diff),
        "reference": sig(_ref(g, p)),
        "source": SPECIAL, "from": p["from"], "deviation": p["deviation"],
        "reason": p["reason"], "evidence": [_evidence_wire(e) for e in p["evidence"]],
        "load": {k: (sig(v) if isinstance(v, float) else v) for k, v in p["load"].items()},
    }  # fmt: skip


def _variation(g: dict, caveats: list[str]) -> list[dict]:
    """The total group's labelled findings (spec §5.4), in one list."""
    d = g["discrepancy"]
    if d["ratio"] is None:
        return sources.measurement_items(caveats)
    lo, hi = d["relative_ci95"] or (None, None)
    out = [sources.item(
        MEASUREMENT, f"measurement interval of L / (lambda W) - 1: {_pct(lo)} to {_pct(hi)} "
        "(gauge sampling, edge straddle, scrape timing, rate lookback)",
    )]  # fmt: skip
    if sysd := g["classification"]["systematic"]:
        out.append(sources.item(
            MEASUREMENT, f"systematic offset L / (lambda W) {_n(sysd['ratio'])} in "
            f"{sysd['windows'][0]} of {sysd['windows'][1]} windows", windows=sysd["windows"],
        ))  # fmt: skip
    cc = g["common_cause"]
    out.append(sources.item(
        COMMON, f"small-system envelope: L and lambda W fluctuate +-{100 * (cc['rel95'] or 0):.0f}% "
        "per window at this traffic",
    ))  # fmt: skip
    out += [
        sources.item(
            t["source"], f"transient window {t['window'][0]}: L / (lambda W) {_n(t['ratio'])} vs "
            f"reference {_n(t['reference'])} ({t['phase']})", window=t["window"],
        )
        for t in g["classification"]["transient"]
    ]  # fmt: skip
    out += [
        sources.item(
            SPECIAL, f"window {p['window'][0]} at a load peak, leaving steady state (promoted from "
            f"{sources.text(p['from'])}): {p['reason']}", window=p["window"], promoted=True,
        )
        for p in g["classification"]["promoted"]
        if p["deviation"] == "within_measurement"  # promoted transients are listed above
    ]  # fmt: skip
    return out + sources.measurement_items(caveats)


def _pooled_source(g: GroupResult) -> str:
    """Source of the whole-range discrepancy: a systematic offset, or one inside the
    measurement interval, is the measurement system's; one carried by transient windows alone
    cannot be told apart at the pooled level (the transients carry their own labels)."""
    if g.systematic is not None or g.pooled.verdict == "consistent":
        return MEASUREMENT
    return UNDETERMINED


def _group(cfg: dict, L_ds: str, labels: dict, g: GroupResult) -> dict:
    p = g.pooled
    judged = [w for w in g.windows if w.ratio is not None]
    ratios = [float(w.ratio) for w in judged]  # type: ignore[arg-type]
    transient = [_transient_wire(g, t) for t in g.transient]
    promoted = [_promoted_wire(g, p) for p in g.promoted]
    discrepancy = {
        "L": sig(p.L), "lambda_W": sig(p.lambda_W), "difference": sig(p.diff),
        "difference_ci95": sig_pair(p.diff_ci) if p.diff_ci else None,
        "ratio": sig(p.ratio), "ratio_ci95": sig_pair(p.ci95) if p.ci95 else None,
        "relative": sig(p.ratio - 1) if p.ratio is not None else None,
        "relative_ci95": _rel(p.ci95),
        "interval": "measurement: gauge sampling, edge straddle, scrape timing, rate lookback",
        "per_window": {
            "windows": len(judged),
            "ratio_range": sig_pair((min(ratios), max(ratios))) if ratios else None,
            "difference_range": sig_pair((
                min(float(w.diff) for w in judged),  # type: ignore[arg-type]
                max(float(w.diff) for w in judged),  # type: ignore[arg-type]
            )) if judged else None,
        },
        "common_cause_rel95": {
            "per_window": sig(g.common_cause.get("rel95")), "whole_range": sig(p.common),
        },
    }  # fmt: skip
    out: dict[str, Any] = {
        "labels": labels or None,
        "discrepancy": discrepancy,
        "verdict": g.verdict,
        "classification": {
            "reference": sig(g.reference),
            "systematic": _systematic_wire(g),
            "transient": transient,
            "promoted": promoted,
            "promotion": _evidence_wire(g.promotion) if g.promotion else None,
        },
        "common_cause": _common_wire(g),
        "ratio": sig(p.ratio),
        "ci95": sig_pair(p.ci95) if p.ci95 else None,
        "L": sig(p.L),
        "L_ci95": sig_pair(p.L_ci) if p.L_ci else None,
        "lambda_per_s": sig(p.lam),
        "W_s": sig(p.W_s),
        "lambda_W": sig(p.lambda_W),
        "lambda_W_ci95": sig_pair(p.lambda_W_ci) if p.lambda_W_ci else None,
        "arrivals": sig(p.arrivals),
        "gauge_samples": p.gauge_samples,
        "sd_terms": {k: sig(v) for k, v in p.sd_terms.items()},
        "bias": {k: sig(v) for k, v in p.bias.items()},
        "window_columns": ["start", "verdict", "ratio", "ci95_lo", "ci95_hi", "L_minus_lambda_W",
                           "source"],
        "windows": [
            [
                iso(w.start_ms),
                w.verdict,
                sig(w.ratio),
                *(sig_pair(w.ci95) if w.ci95 else [None, None]),
                sig(w.diff),
                w.source,
            ]
            for w in g.windows
        ],
        "flagged_windows": [
            {
                "window": _span(w),
                "verdict": w.verdict,
                "ratio": sig(w.ratio),
                "ci95": sig_pair(w.ci95) if w.ci95 else None,
                "difference": sig(w.diff),
                "source": w.source,
                "flags": w.flags,
            }
            for i, w in enumerate(g.windows)
            if i in g.flagged
        ],
    }  # fmt: skip
    if p.reason:
        out["reason"] = p.reason
    if g.growing:
        out["growing"] = {
            "slope_per_h": sig(g.growing["slope_per_h"]),
            "slope_ci": sig_pair(g.growing["slope_ci"]),
            "growing": g.growing["growing"],
        }
    if p.ratio is not None and p.ci95 is not None:
        params = {
            "group": labels or "total", "window": _span(p), "by": cfg["by"],
            "datasets": cfg["datasets"], "unit": cfg["unit"],
        }  # fmt: skip
        src = _pooled_source(g)
        ev = [
            statistic(L_ds, "littles_law_ratio", sig(p.ratio), sig_pair(p.ci95), METHOD,
                      {**params, "L": sig(p.L), "lambda_W": sig(p.lambda_W)}, source=src),
            statistic(L_ds, "littles_law_discrepancy", sig(p.ratio - 1), _rel(p.ci95),
                      "relative discrepancy L / (lambda W) - 1 over the whole range, with its "
                      "measurement interval (source: measurement system)",
                      {**params, "difference": sig(p.diff),
                       "difference_ci95": sig_pair(p.diff_ci) if p.diff_ci else None,
                       "common_cause_rel95": sig(p.common)}, source=src),
            statistic(L_ds, "mean_concurrency_L", sig(p.L), sig_pair(p.L_ci),
                      "time average of the gauge; successive-difference sampling error, "
                      "Poisson-occupancy floor, edge straddle", params),
            statistic(L_ds, "lambda_times_W", sig(p.lambda_W), sig_pair(p.lambda_W_ci),
                      "rate(counter) x rate(_sum)/rate(_count); scrape-timing error at the "
                      "window edges (lambda and W linearly), lookback bound", params),
        ]  # fmt: skip
        sysw = _systematic_wire(g)
        if sysw and g.systematic is not None:
            ev.append(statistic(
                L_ds, "littles_law_systematic_offset", sysw["ratio"], sysw["ci95"],
                "L / (lambda W) shared by the non-transient windows (equal weights; error the "
                "larger of measurement and window spread); source: measurement system",
                {**params, "window": _span(g.systematic), "windows": sysw["windows"],
                 "drifting": sysw["drifting"]}, source=MEASUREMENT,
            ))  # fmt: skip
        for t in transient:
            ev.append(statistic(
                L_ds, "littles_law_transient", t["relative"],
                [None if v is None else sig(v - 1) for v in t["ci95"]] if t["ci95"] else None,
                "relative discrepancy L / (lambda W) - 1 of a transient window, measurement "
                f"interval; source: {t['source'].replace('_', ' ')}",
                {**params, "window": t["window"], "reference": t["reference"],
                 "vs_reference": t["vs_reference"], "phase": t["phase"],
                 "common_cause_rel95": t["common_cause_rel95"]}, source=t["source"],
            ))  # fmt: skip
        for p in promoted:
            for e in p["evidence"]:
                if not e["significant"]:
                    continue
                ev.append(statistic(
                    L_ds, f"littles_law_{e['kind']}", e["value"], e["interval"],
                    PROMOTION_METHOD[e["kind"]],
                    {**params, "window": p["window"], "promoted_from": p["from"],
                     **{k: v for k, v in e.items()
                        if k not in ("kind", "value", "interval", "significant")}},
                    source=SPECIAL,
                ))  # fmt: skip
        out["evidence"] = ev
    return out


PROMOTION_METHOD = {
    "backlog_growth": (
        "backlog change N(end) - N(start) over a load-peak window, requests (the gauge; with "
        "arrivals - completions when the counter counts arrivals, the smaller); interval: the readings' "
        "spread and counter scrape timing; promotion when z = value / sqrt(2 var N) clears "
        "Cantelli's bound k (var N from the gauge outside the peak's episode); source: special "
        "cause"
    ),
    "latency_rise": (
        "rise of the mean latency W across two consecutive windows into or through a load peak, "
        "seconds; interval from the windows' standard errors around their own trend; promotion "
        "when both rises clear their t thresholds (n_eff df, Bonferroni); source: special cause"
    ),
    "peak_growth": (
        "growth of |L / (lambda W) / reference - 1| from the first load peak to this one; "
        "interval from the two windows' measurement sds; promotion when Kendall's S over the "
        "peaks is significant (exact one-sided p, Bonferroni over groups); source: special cause"
    ),
}
