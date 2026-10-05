"""analyze on born error-span series, the shape of eval round 4's payment-failure run (bead
7thi; tests/fixtures/evals/payment-failure.live-sonnet-4.{stream.jsonl,snapshot.json}).

The run's analyze(d2) call: d2 = sum by (service_name, span_name) (increase(calls{status_code=
"STATUS_CODE_ERROR", service_name=~"payment|checkout|frontend"}[1m])), 16:25-16:55 at 60 s
(31 steps). The stream keeps only the first 300 characters of each result, so the data is
reconstructed from what the run recorded: the demo stack reported from 16:27 (d5: the UNSET
sibling has no samples before 16:27), the error series of the PlaceOrder -> Charge path were
born at 16:40 ("counter first seen 16:40", d4's first row 16:40 = 0), errors 16:41-16:45
(61 by 16:45, none from 16:46), and the data ends at the query's "now" ~16:48 (no samples since
16:48/16:49 although the range runs to 16:55). tcp.connect and the flagd EventStream error
series have no live sibling in the complement.

Round 4: every series came back insufficient_data "skipped: too_few_points" although the
absent_as_zero fill ran: the fill gave the path's series 22 points (16:27-16:48), and analyze
required spectrum's 32.

0vg7: the departure from the 13-step zero baseline is reported under two models. Poisson
(independent errors): p = (9/22)^61 ~ 2e-24. Clustered errors (the cautious model the label
rests on): the judged steps' own dispersion D ~ 13 (the live siblings' traffic is steadier,
D < 1, and only ever raises D) leaves ~4.7 independent clusters, p = (9/22)^4.7 ~ 0.015 >= 0.01:
undetermined. 13 quiet minutes cannot tell one burst of clustered errors from a change; a longer
zero baseline can (tests/unit/test_diagnostics.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np

from telemetry_nerd.analysis.born_counters import CAVEAT
from telemetry_nerd.evals.score import score
from telemetry_nerd.evals.truth import load_truth
from telemetry_nerd.model.time import parse_time
from tests.unit.fakes import FakeSource, make_service
from tests.unit.test_born_counters import _table

M = 60_000
T0 = parse_time("2026-10-03T16:25:00Z", 0)
T_END = parse_time("2026-10-03T16:55:00Z", 0)
NOW = parse_time("2026-10-03T16:48:40Z", 0)
SEL = 'service_name=~"payment|checkout|frontend"'
CALLS = "traces_span_metrics_calls_total"
D2 = f'sum by (service_name, span_name) (increase({CALLS}{{status_code="STATUS_CODE_ERROR",{SEL}}}[1m]))'
D3 = f'sum by (service_name, span_name) (increase({CALLS}{{status_code!="STATUS_CODE_ERROR",{SEL}}}[1m]))'

PATH = [  # the failing PlaceOrder -> Charge path: an OK/UNSET sibling throughout
    {"service_name": "frontend", "span_name": "executing api route (pages) /api/checkout"},
    {"service_name": "frontend", "span_name": "grpc.oteldemo.CheckoutService/PlaceOrder"},
    {"service_name": "checkout", "span_name": "oteldemo.CheckoutService/PlaceOrder"},
    {"service_name": "checkout", "span_name": "oteldemo.PaymentService/Charge"},
    {"service_name": "payment", "span_name": "oteldemo.PaymentService/Charge"},
    {"service_name": "payment", "span_name": "charge"},
]
LONERS = [  # error series with no sibling in the complement: genuinely too few points
    {"service_name": "payment", "span_name": "tcp.connect"},
    {"service_name": "frontend", "span_name": "flagd.evaluation.v1.Service/EventStream"},
]
LIVE, BORN, FIRST_ERR, LAST_ERR = (
    parse_time(f"2026-10-03T16:{m}:00Z", 0) for m in ("27", "40", "41", "45")
)
BURST = [9.0, 14.0, 12.0, 13.0, 13.0]  # 61 errors 16:41-16:45


class Round4(FakeSource):
    def __init__(self):
        super().__init__(name="default", n_series=1)
        self.exprs: list[str] = []

    async def fetch(self, expr, rng, step_ms):
        self.calls += 1
        self.exprs.append(expr)
        ts = np.arange(rng.start_ms, min(rng.end_ms, NOW) + 1, step_ms)
        rows = []
        if expr == D2:
            for lb in PATH:
                t = ts[ts >= BORN]
                y = np.zeros(t.size)
                y[(t >= FIRST_ERR) & (t <= LAST_ERR)] = BURST
                rows.append((lb, t, y))
            for lb, (a, b) in zip(LONERS, [("30", "33"), ("44", "45")], strict=True):
                t0, t1 = (parse_time(f"2026-10-03T16:{m}:00Z", 0) for m in (a, b))
                t = ts[(ts >= t0) & (ts <= t1)]
                rows.append((lb, t, np.ones(t.size)))
        elif expr == D3:
            rng_ = np.random.default_rng(4)
            for lb in PATH:
                t = ts[ts >= LIVE]
                rows.append((lb, t, 13.0 + rng_.normal(scale=1.5, size=t.size).round()))
        return _table(rows)

    fetch_values = fetch


def _run(tmp_path):
    src = Round4()
    svc = make_service(tmp_path, source=src, clock=lambda: NOW)
    d = asyncio.run(svc.query(D2, start=str(T0), end=str(T_END), step="60s"))["dataset"]
    return src, svc, asyncio.run(svc.analyze_profiled(d))


def test_round4_born_error_series_are_analyzed(tmp_path):
    src, _, out = _run(tmp_path)
    assert D3 in src.exprs  # the live sibling was fetched
    assert CAVEAT in out["caveats"]
    by = {s["labels"]["span_name"] + "@" + s["labels"]["service_name"]: s for s in out["series"]}
    for lb in PATH:
        s = by[lb["span_name"] + "@" + lb["service_name"]]
        assert not s["reasons"][0].startswith("skipped"), s  # judged, not skipped
        assert s["absent_as_zero"]["before_first_point"] == (BORN - LIVE) // M
        dep = s["stability"]["departure"]
        assert dep["models"]["clustered"]["dispersion_sibling"] is not None  # sibling read
        # undecided under the cautious model: neither labelled a shift nor called stable; it
        # was tested, so not "insufficient data" either (7f15): the models disagree
        assert s["verdict"] == "undetermined" and "level_shifted" not in s["also"], s
        und = [v for v in s["variation"] if v["source"] == "undetermined"]
        assert und and "under a Poisson model" in und[0]["finding"], s
    for lb in LONERS:  # no sibling: never read as 0, still too few points
        s = by[lb["span_name"] + "@" + lb["service_name"]]
        assert s["verdict"] == "insufficient_data"
        assert s["reasons"] == ["skipped: too_few_points"]


def test_round4_departure_is_citable_evidence(tmp_path):
    _, svc, out = _run(tmp_path)
    s = next(s for s in out["series"] if s["labels"] == PATH[2])  # checkout PlaceOrder
    dep = s["stability"]["departure"]
    assert dep["at"] == "2026-10-03T16:41:00+00:00" and dep["events"] == sum(BURST)
    # telemetry-nerd-k9sn: NOW (16:48:40) is before T_END (16:55), so the default baseline
    # splits the OBSERVED span (T0..NOW) in half -> baseline ends 16:37, inside the LIVE..BORN
    # zero fill: n_baseline = (16:37 - LIVE) / M, n_judged = (NOW - 16:37) / M + 1
    assert (dep["n_baseline"], dep["n_judged"]) == (10, 12)
    assert "future_range" in out["caveats"]
    ev = dep["evidence"]
    assert ev["name"] == "departure_from_zero" and ev["source"] == "undetermined"
    # both models, each named; the label rests on the clustered one. The module docstring's
    # 0vg7 numbers (p~2e-24, p_clustered~0.015) are the historical eval run's own, computed over
    # its (buggy) 13-step zero baseline; the k9sn fix shrinks the default baseline to the
    # observed span (10 steps here), moving the split and so these numbers
    assert dep["p"] < 1e-10 and 0.1 < dep["p_clustered"] < 0.2
    assert ev["params"]["p_poisson"] < 1e-10 and ev["params"]["dispersion_source"] == "judged"
    assert dep["label_rests_on"] == "clustered" and "Poisson" in ev["method"]
    assert dep["summary"].startswith("under a Poisson model (independent events) p=8.8e-17;")
    assert "allowing clustered events" in dep["summary"]
    # the op recorded it: cited back without its source, the source is derived (tcfz path too)
    bare = {k: v for k, v in ev.items() if k != "source"} | {"method": "analyze"}
    assert svc.datasets.statistic_sources(
        bare["dataset"], bare["name"], bare["method"], bare["value"]
    ) == {"undetermined"}


def test_round4_payment_rescored_with_the_departure_cited():
    """Counterfactual offline re-score: the recorded f1 cites panels only (round 4 had no
    analyze statistic: 11/12, source_label fail, kept as is in test_evals). Citing the
    departure statistic analyze now returns for d2: under the cautious model it is undetermined
    (0vg7; 13 zero minutes cannot show special cause under clustered errors). The finding keeps
    the op's label, which is honest, not a model mistake: source_label passes since eval round 5
    (12/12); calling it special cause in the claim would fail (test_evals)."""
    ev_dir = Path(__file__).parents[1] / "fixtures/evals"
    snap = json.loads((ev_dir / "payment-failure.live-sonnet-4.snapshot.json").read_text())
    f1 = next(f for f in snap["workspace"]["findings"] if f["id"] == "f1")
    f1["evidence"].append({
        "kind": "statistic", "dataset": "d2", "name": "departure_from_zero", "value": 6.778,
        "interval": [4.751, 9.35], "exact": False, "method": "analyze", "params": {},
        "source": "undetermined",
    })  # fmt: skip
    f1["source_flags"] = []
    rep = score(snap, load_truth(ev_dir / "payment-failure.live-sonnet-4.truth.json"))
    st = {c.id: c.status for c in rep.checks}
    assert [k for k, v in st.items() if v == "fail"] == []
    f1s = next(f for f in rep.findings if f.id == "f1")
    assert f1s.source_ok and f1s.honest_undetermined
    assert (rep.passed, rep.applicable) == (12, 12)
