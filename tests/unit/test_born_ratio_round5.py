"""analyze on a hand-written error ratio whose error series is born at the fault, the shape of
eval round 5's payment-failure run (bead wr6j; tests/fixtures/evals/payment-failure.live-sonnet-5.
{stream.jsonl,snapshot.json}).

The run's analyze(d3, baseline 20:40-21:12) call: d3 = sum by (service_name) (rate(calls{status_
code="STATUS_CODE_ERROR", service_name=~"payment|checkout|frontend"}[1m])) / sum by (service_name)
(rate(calls{service_name=~"payment|checkout|frontend"}[1m])), 20:40-21:40 at 60 s (61 steps). The
stream keeps only the first 300 characters of each result, so the data is reconstructed from
what the run recorded: payment's error series came back too_gappy (16-30 of 61 points: born
before the fault, from a stray error), checkout's and frontend's too_few_points (born at the
fault: paymentFailure 100% 21:28:39-21:33:39); the data ends at the query's "now" ~21:37.

PromQL's division drops every step where the error series does not exist yet, so the ratio is
born at the fault exactly like the error counter (7thi fixed counts, not ratios). analyze now
computes the ratio from its parts: errors / calls at each step where calls > 0, the error
series read as 0 outside its observed lifetime there (its live sibling is the denominator)."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from telemetry_nerd.analysis.born_counters import CAVEAT, RATIO_ASSUMPTION
from telemetry_nerd.model.time import parse_time
from tests.unit.fakes import FakeSource, make_service
from tests.unit.test_born_counters import _table

M = 60_000
T0 = parse_time("2026-10-03T20:40:00Z", 0)
T_END = parse_time("2026-10-03T21:40:00Z", 0)
NOW = parse_time("2026-10-03T21:37:20Z", 0)
SEL = 'service_name=~"payment|checkout|frontend"'
CALLS = "traces_span_metrics_calls_total"
NUM = f'sum by (service_name) (rate({CALLS}{{status_code="STATUS_CODE_ERROR",{SEL}}}[1m]))'
DEN = f"sum by (service_name) (rate({CALLS}{{{SEL}}}[1m]))"
D3 = f"{NUM} / {DEN}"
BASE_END = "2026-10-03T21:12:00Z"

SERVICES = ("payment", "checkout", "frontend")
BORN = {  # the error series' first point
    "payment": parse_time("2026-10-03T21:14:00Z", 0),  # a stray error before the fault
    "checkout": parse_time("2026-10-03T21:29:00Z", 0),
    "frontend": parse_time("2026-10-03T21:29:00Z", 0),
}
FAULT = (parse_time("2026-10-03T21:29:00Z", 0), parse_time("2026-10-03T21:34:00Z", 0))
SHARE = {"payment": 0.5, "checkout": 0.3, "frontend": 0.08}  # error share during the fault


def parts(ts):
    """(errors/s, calls/s) per service on the grid `ts`, the error series only from its birth."""
    rng = np.random.default_rng(5)
    out = {}
    for i, svc in enumerate(SERVICES):
        calls = (4.0 + i) * (1 + 0.05 * rng.normal(size=ts.size))
        err = np.zeros(ts.size)
        fault = (ts >= FAULT[0]) & (ts <= FAULT[1])
        err[fault] = SHARE[svc] * calls[fault]
        if svc == "payment":
            err[ts == BORN["payment"]] = 0.02  # the stray error that created the series
        out[svc] = (err, calls, ts >= BORN[svc])
    return out


class Round5(FakeSource):
    def __init__(self):
        super().__init__(name="default", n_series=1)
        self.exprs: list[str] = []

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        self.exprs.append(expr)
        ts = np.arange(rng.start_ms, min(rng.end_ms, NOW) + 1, step_ms)
        rows = []
        for svc, (err, calls, born) in parts(ts).items():
            lb = {"service_name": svc}
            if expr == D3:  # PromQL: no value where the numerator series does not exist
                rows.append((lb, ts[born], err[born] / calls[born]))
            elif expr == NUM:
                rows.append((lb, ts[born], err[born]))
            elif expr == DEN:
                rows.append((lb, ts, calls))
        return _table(rows)

    fetch_values = fetch


def _run(tmp_path):
    src = Round5()
    svc = make_service(tmp_path, source=src, clock=lambda: NOW)
    d = asyncio.run(svc.query(D3, start=str(T0), end=str(T_END), step="60s"))["dataset"]
    out = asyncio.run(svc.analyze_profiled(d, "2026-10-03T20:40:00Z", BASE_END))
    return src, svc, d, out


def test_round5_born_error_ratio_is_analyzed(tmp_path):
    src, _, _, out = _run(tmp_path)
    assert NUM in src.exprs and DEN in src.exprs  # the parts were fetched
    assert CAVEAT in out["caveats"]
    r = out["ratio"]
    assert r["numerator"]["expr"] == NUM and r["denominator"]["expr"] == DEN
    assert r["assumption"] == RATIO_ASSUMPTION
    by = {s["labels"]["service_name"]: s for s in out["series"]}
    assert set(by) == set(SERVICES)
    for svc in SERVICES:
        s = by[svc]
        assert not s["reasons"][0].startswith("skipped"), s  # judged, not skipped
        z = s["absent_as_zero"]
        assert z["of"] == "numerator" and z["live_sibling"]["expr"] == DEN
        assert z["before_first_point"] == (BORN[svc] - T0) // M
        # the errors after the 0 baseline are tested on the numerator's counts, the
        # denominator the traffic they are a share of (exposure and sibling dispersion). Six
        # minutes of errors after 32 quiet ones: under independent errors p ~ 1e-60 or less,
        # allowing clustered errors (the judged steps' own dispersion, the cautious model the
        # label rests on, 0vg7) ~1.6 clusters, p ~ 0.3: undetermined, never "stable" and never
        # skipped (wr6j); a longer quiet baseline would decide it
        dep = s["stability"]["departure"]
        assert dep["p"] < 1e-50 and dep["p_clustered"] > 0.01, dep
        assert dep["models"]["clustered"]["dispersion_sibling"] is not None
        assert dep["evidence"]["source"] == "undetermined"
        assert s["verdict"] == "undetermined", s
        und = [v for v in s["variation"] if v["source"] == "undetermined"]
        assert und and "numerator units of the ratio" in und[0]["finding"], s


def test_round5_ratio_series_values(tmp_path):
    """The ratio is 0 (not missing) before the error series was born, wherever calls report,
    and errors / calls after."""
    _, svc, d, _ = _run(tmp_path)
    prep, _, _, _ = svc.diagnostics.run(d, T0, parse_time(BASE_END, 0))
    rows = {lab["service_name"]: (ts, y) for lab, ts, y in prep.series.values()}
    ts, y = rows["checkout"]
    assert ts[0] == T0 and ts[-1] == (NOW // M) * M  # every step the calls report
    assert np.all(y[ts < BORN["checkout"]] == 0)
    fault = (ts >= FAULT[0]) & (ts <= FAULT[1])
    assert np.allclose(y[fault], SHARE["checkout"])


def test_a_ratio_that_cannot_be_decomposed_gets_the_rewrite_hint(tmp_path):
    """Without the parts (here: the source cannot fetch them), the skipped series say how to
    rewrite the ratio so the numerator is 0 where it does not exist yet."""

    class NoParts(Round5):
        async def fetch(self, expr, rng, step_ms):
            if expr != D3:
                from telemetry_nerd.sources.base import SourceError

                raise SourceError("unavailable")
            return await super().fetch(expr, rng, step_ms)

        fetch_values = fetch

    svc = make_service(tmp_path, source=NoParts(), clock=lambda: NOW)
    d = asyncio.run(svc.query(D3, start=str(T0), end=str(T_END), step="60s"))["dataset"]
    out = asyncio.run(svc.analyze_profiled(d, "2026-10-03T20:40:00Z", BASE_END))
    skipped = [s for s in out["series"] if s["reasons"][0].startswith("skipped")]
    assert {s["labels"]["service_name"] for s in skipped} == set(SERVICES)
    for s in skipped:
        assert f"({NUM} or {DEN} * 0) / ({DEN})" in s["hint"], s


def test_a_sustained_error_ratio_after_a_quiet_baseline_is_a_level_shift(tmp_path, monkeypatch):
    """Errors on every step from 21:16 to the end (born then, every service): steady errors
    have a small dispersion, so the cautious model decides too: special cause, a level shift
    of the ratio from its 0 baseline."""
    mod = __import__(__name__, fromlist=["FAULT"])
    start = parse_time("2026-10-03T21:16:00Z", 0)
    monkeypatch.setattr(mod, "FAULT", (start, T_END))
    monkeypatch.setattr(mod, "BORN", dict.fromkeys(SERVICES, start))
    _, _, _, out = _run(tmp_path)
    for s in out["series"]:
        dep = s["stability"]["departure"]
        assert dep["source"] == "special_cause" and dep["p_clustered"] < 0.01, dep
        assert s["verdict"] == "level_shifted", s["reasons"]


def test_born_ratio_reading():
    from telemetry_nerd.analysis.born_counters import born_ratio

    counter = lambda m: "counter"
    r = born_ratio(D3, counter)
    assert r is not None and (r.numerator, r.denominator) == (NUM, DEN)
    ok = 'sum(rate(a{code!="500"}[1m]))'
    assert born_ratio(f'sum(rate(a{{code="500"}}[1m])) / {ok}', counter) is not None  # complement
    for expr in (
        'sum(rate(a{code="500"}[1m])) / sum(rate(a[5m]))',  # another window
        'sum(rate(a{code="500"}[1m])) / sum(rate(b[1m]))',  # another metric
        'sum(rate(a{code="500"}[1m])) / on(x) sum(rate(a[1m]))',  # vector matching
        'sum(rate(a{code="500"}[1m])) / sum by (x) (rate(a[1m]))',  # another grouping
        'sum(rate(a{code="500",x="1"}[1m])) / sum(rate(a{x="2"}[1m]))',  # another identity
        "sum(rate(a[1m])) / sum(rate(a[1m]))",  # nothing born
        'sum(rate(a{code="500"}[1m])) / 60',
    ):
        assert born_ratio(expr, counter) is None, expr
    assert born_ratio(D3, lambda m: "gauge") is None


def test_departure_exposure_replaces_the_step_share():
    """One error probability per call: with twice the traffic in the judged steps, the judged
    share of the exposure is 2/3, not 1/2, so the same errors are less surprising."""
    from telemetry_nerd.analysis.diagnostics import departure_from_zero

    ts = np.arange(32, dtype=np.int64) * M
    y = np.r_[np.zeros(16), np.full(16, 2.0)]
    base = np.arange(32) < 16
    flat = departure_from_zero(ts, y, base, 1.0, step_ms=M)
    busy = departure_from_zero(ts, y, base, 1.0, step_ms=M, exposure=np.r_[np.ones(16), np.full(16, 2.0)])  # fmt: skip
    assert flat.p == pytest.approx(0.5**32) and busy.p == pytest.approx((2 / 3) ** 32)
    assert busy.p_clustered > flat.p_clustered
