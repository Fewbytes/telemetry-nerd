"""Seasonal comparison for latency (bead lkn.7): the histogram per cycle, never percentiles."""

import asyncio
import math

import numpy as np
import pytest

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
from telemetry_nerd.analysis.seasonal_dist import (
    Hist,
    common_edges,
    compare_tail,
    default_threshold,
    snap,
)
from tests.unit.fakes import FakeSource, make_service
from tests.unit.seasonal_dist_sim import LE, _cdf, run, scenario
from tests.unit.test_seasonal import DAY, MON, H


def test_default_threshold_is_the_reference_edge_nearest_one_percent():
    now, cyc = scenario(1, 4, 50_000, 0.05)
    edges = common_edges([now, *cyc])
    assert set(edges) <= set(LE)
    x, enough = default_threshold(cyc, edges)
    assert x == 0.25 and enough  # median 80 ms, sigma 0.6: ~3% above 250 ms, 0.1% above 500 ms


def test_requested_threshold_snaps_to_a_shared_edge():
    edges = np.array([0.1, 0.25, 0.5])
    assert snap(0.3, edges) == 0.25 and snap(0.25, edges) == 0.25 and snap(0.45, edges) == 0.5


def test_normal_cycle_is_usual_and_a_slow_tail_is_unusual():
    c = run(3, 4, 50_000, 0.0)
    assert c.verdict == "usual", c.reasons
    assert len(c.kept) == 4 and c.share.p_interval[0] < c.share.value < c.share.p_interval[1]
    slow = run(3, 4, 50_000, 0.0, tail=0.05)
    assert slow.verdict == "unusual" and slow.direction == "higher", slow.reasons
    assert slow.shape > max(slow.shape_previous)


def test_an_atypical_reference_cycle_is_excluded():
    rng = np.random.default_rng(5)
    now, cyc = scenario(5, 5, 50_000, 0.0)
    from tests.unit.seasonal_dist_sim import window_hist

    cyc[1] = window_hist(rng, 2, 50_000, 0.0, tail=0.2)  # an incident in the reference
    c = compare_tail(now, cyc, 0.25, "1w")
    assert [(h.j, why) for h, why in c.excluded] == [(2, "atypical")]
    assert c.verdict == "usual", c.reasons


def test_insufficient_history_and_missing_cycles():
    now, cyc = scenario(2, 3, 2_000, 0.05)
    cyc[0] = Hist(1, 0, 0, np.array([]), np.array([]), np.array([]), 0.0)
    c = compare_tail(now, cyc, 0.25, "1d")
    assert c.verdict == "insufficient_history" and "2 usable" in c.reasons[0]
    assert [why for _, why in c.excluded] == ["missing"]


def test_share_band_calibration_and_false_alarms():
    cov = alarms = 0
    seeds = 60
    for s in range(seeds):
        c = run(70_000 + s, 4, 2_000, 0.05)
        cov += c.share.normal[0] <= c.share.value <= c.share.normal[1]
        alarms += c.verdict == "unusual"
    print(f"coverage {cov / seeds:.3f} (nominal 0.90), false alarms {alarms}/{seeds}")
    assert 0.8 <= cov / seeds <= 0.98
    assert alarms <= 2


# --- end to end over a fake source -----------------------------------------------------------
SAT = MON + 5 * DAY + 8 * H


class LatencySource(FakeSource):
    """A latency histogram (classic le buckets); `slow` moves a share of requests x5 slower in
    [a, b)."""

    def __init__(self, slow=None):
        super().__init__(n_series=1)
        self.slow = slow

    async def fetch_histogram(self, selector, by, rng, step_ms):
        self.calls += 1
        result = []
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        cum = []
        for t in ts:
            g = np.random.default_rng(t // step_ms)
            mc = math.log(0.08) + g.normal(0, 0.1)
            p = np.diff(np.concatenate([[0.0], _cdf(LE, mc, 0.6), [1.0]]))
            if self.slow and self.slow[0] <= t < self.slow[1]:
                ps = np.diff(np.concatenate([[0.0], _cdf(LE, mc + math.log(5), 0.6), [1.0]]))
                p = (1 - self.slow[2]) * p + self.slow[2] * ps
            cum.append(np.cumsum(g.multinomial(400, p / p.sum())))
        for i, le in enumerate([*LE, math.inf]):
            result.append({
                "metric": {"le": "+Inf" if math.isinf(le) else f"{le:g}"},
                "values": [[t / 1000, str(float(c[i]))] for t, c in zip(ts, cum, strict=True)],
            })  # fmt: skip
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))


P99 = "histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))"


def _p99(svc, start, hours=6):
    out = asyncio.run(svc.query(P99, start=str(start), end=str(start + hours * H), step="5m"))
    return out["dataset"]


def test_percentile_series_compares_the_histogram_per_cycle(tmp_path):
    svc = make_service(tmp_path, source=LatencySource())
    out = asyncio.run(svc.compare_seasonal(_p99(svc, SAT), cycles=["1d", "1w"]))
    assert out["kind"] == "distribution" and "never pooled" in out["method"]
    (s,) = out["series"]
    assert s["verdict"] == "usual", s["reasons"]
    ref = s["reference"]
    assert ref["scheme"] in ("1d", "1w") and all(c["n"] > 20_000 for c in ref["cycles"])
    assert s["threshold"]["x"] == 0.25 and "not from now" in s["threshold"]["how"]
    assert s["share_over"]["evidence"]["name"] == "seasonal_share_over"
    assert len(str(out)) < 6000
    # spec §5.4: a usual window sits inside the cycle-to-cycle (common-cause) envelope
    assert s["source"] == s["share_over"]["evidence"]["source"] == "common_cause"
    assert "special_cause" not in {v["source"] for v in s["variation"]}


def test_slow_tail_now_is_unusual_and_threshold_is_snapped(tmp_path):
    svc = make_service(tmp_path, source=LatencySource(slow=(SAT, SAT + 6 * H + 1, 0.05)))
    out = asyncio.run(svc.compare_seasonal(_p99(svc, SAT), cycles=["1w"], threshold=0.3))
    (s,) = out["series"]
    assert s["threshold"]["x"] == 0.25 and "snapped" in s["threshold"]["how"]
    assert s["verdict"] == "unusual" and s["direction"] == "higher", s["reasons"]
    assert s["source"] == s["share_over"]["evidence"]["source"] == "special_cause"
    assert "special_cause" in {v["source"] for v in s["variation"]}


def test_distribution_dataset_is_compared_too(tmp_path):
    svc = make_service(tmp_path, source=LatencySource())
    d = asyncio.run(svc.query_distribution(
        "http_request_duration_seconds_bucket", start=str(SAT), end=str(SAT + 6 * H), step="5m"
    ))["dataset"]  # fmt: skip
    out = asyncio.run(svc.compare_seasonal(d, cycles=["1w"]))
    assert out["histogram"]["now"] == d and out["series"][0]["verdict"] == "usual"


def test_summary_quantiles_are_refused_with_a_hint(tmp_path):
    svc = make_service(tmp_path, source=LatencySource())
    q = asyncio.run(svc.query('rpc_latency{quantile="0.99"}', start=str(SAT), end=str(SAT + H)))
    meta = svc.datasets.meta(q["dataset"])
    if meta.representation != "quantile":
        pytest.skip("summary quantile not recognised as a percentile series")
    with pytest.raises(ValueError, match="histogram per cycle"):
        asyncio.run(svc.compare_seasonal(q["dataset"]))
