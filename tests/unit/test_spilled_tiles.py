"""Scrapes on the bucket edges at a step of one scrape interval (uup): a bucket a scrape spilled
out of holds no sample, its neighbour two. The value the source gives there is kept (marked by
count 0) where it is the expression's own, so consumers reading the dataset's values (analyze,
spectrum, fleet, seasonal) see every bucket bucket_state calls covered; a gap stays missing."""

import asyncio
import re

import httpx
import numpy as np
import respx

from telemetry_nerd.model.time import parse_duration
from telemetry_nerd.sources.promql import PromQLSource

from .fakes import NOW, make_service

BASE = "http://vm.test"
ROUTE = {"host": "vm.test", "path": "/api/v1/query_range"}
SCRAPE_S = 15
RATE = 10.0  # counter increments per second


def _scrapes(outage: range = range(0)) -> np.ndarray:
    """Scrape k is due at 15k s: it lands half a second early, except 1 in 4 lands half a
    second late (in the next bucket). None arrive during `outage` (scrape indices)."""
    k = np.arange((NOW // 1000 - 7200) // SCRAPE_S, NOW // 1000 // SCRAPE_S + 1)
    t = k * SCRAPE_S + np.where(k % 4 == 1, 0.5, -0.5)
    return t[~np.isin(k, list(outage))]


def _vm(scrapes: np.ndarray, wave: float = 0.0):
    """A VictoriaMetrics stand-in for one counter `x` at RATE/s: count_over_time, increase and
    rate over a window, without extrapolation, from the last sample before the window; a window
    holding no sample after one gives 0 (VM's 'no change'), also through an outage. `wave`: a
    5 min swing of that many increments on top of the steady rate."""

    def value(t: float) -> float:
        return RATE * t + wave * np.sin(2 * np.pi * t / 300)

    def at(t: float, window: float, fn: str) -> float | None:
        inside = scrapes[(scrapes > t - window) & (scrapes <= t)]
        before = scrapes[scrapes <= t - window]
        if fn == "count":
            return float(inside.size) if inside.size else None
        if not before.size:
            return None
        if not inside.size:
            return 0.0
        dv = value(inside[-1]) - value(before[-1])
        return dv if fn == "increase" else dv / (inside[-1] - before[-1])

    def respond(request):
        p = request.url.params
        q = p["query"]
        start, end, step = float(p["start"]), float(p["end"]), float(p["step"].rstrip("s"))
        fn = "count" if "count_over_time" in q else "increase" if "increase(" in q else "rate"
        window = parse_duration(re.search(r"\(x\[(\w+)\]", q).group(1)) / 1000
        ts = np.arange(start, end + 1e-9, step)
        values = [[t, str(v)] for t in ts if (v := at(t, window, fn)) is not None]
        if fn == "count":
            result = [{"metric": {}, "values": values}]
        else:
            result = [{"metric": {"rollup": r}, "values": values} for r in ("avg", "min", "max")]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    return respond


def _query(tmp_path, expr: str, outage: range = range(0), wave: float = 0.0) -> tuple:
    respx.get(**ROUTE).mock(side_effect=_vm(_scrapes(outage), wave))
    src = PromQLSource("default", BASE, flavor="victoriametrics", resolution_ms=SCRAPE_S * 1000)
    svc = make_service(tmp_path, src)
    out = asyncio.run(svc.query(expr, start="now-40m", end="now", step="15s"))
    meta, res = svc.datasets.get(out["dataset"])
    rows = res.buckets.to_pylist()
    return svc, out["dataset"], meta, {r["ts_ms"] // 1000: (r["count"], r["avg"]) for r in rows}


@respx.mock
def test_increase_tiles_keep_the_spilled_tiles_zero_and_drop_an_outage(tmp_path):
    first = (NOW // 1000 - 2400) // SCRAPE_S
    outage = range(first + 100, first + 112)
    _, _, _, got = _query(tmp_path, "sum(increase(x[15s]))", outage)
    before = {t: v for t, v in got.items() if t < (first + 100) * SCRAPE_S}
    spilled = [t for t, (c, _) in before.items() if c == 0]
    assert spilled and all(before[t][1] == 0.0 for t in spilled)
    # analyze / spectrum / fleet read the values: per tile the counter rose RATE x 15 s on
    # average; without the spilled tiles it read 4/3 of that (the next tile holds two scrapes)
    assert np.mean([v for _, v in before.values()]) == RATE * SCRAPE_S
    # the outage is missing data, not a run of zeros (VM's 'no change' there is no evidence)
    gap = [t for t in got if (first + 100) * SCRAPE_S <= t < (first + 112) * SCRAPE_S]
    assert gap == []


@respx.mock
def test_rate_over_a_wider_window_is_not_read_as_gappy_where_scrapes_spilled(tmp_path):
    svc, ds, meta, got = _query(tmp_path, "sum(rate(x[1m]))", wave=300.0)
    # every bucket has the expression's own value (the window holds samples), spilled or not
    assert len(got) == (meta.end_ms - meta.start_ms) // meta.step_ms + 1
    assert any(c == 0 for c, _ in got.values())
    assert all(v > 0 for _, v in got.values())
    out = svc.spectrum(ds)
    assert "gaps" not in out["caveats"]
    # the query summary does not count them as gaps either
    summary = asyncio.run(svc.query("sum(rate(x[1m]))", start="now-40m", end="now", step="15s"))
    assert [s["gaps"] for s in summary["summary"]["series"]] == [0]
    assert "gaps" not in summary["summary"]["caveats"]


@respx.mock
def test_a_rate_tile_without_a_sample_stays_missing(tmp_path):
    # VM's rate over a window holding no sample is 0: not the counter's rate, so not kept
    _, _, _, got = _query(tmp_path, "sum(rate(x[15s]))")
    assert got and all(c > 0 for c, _ in got.values())
    assert np.mean([v for _, v in got.values()]) == RATE
