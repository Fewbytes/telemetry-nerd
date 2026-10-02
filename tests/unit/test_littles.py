"""Little's law check, statistics layer (czt.2): seeded M/M/c simulations."""

import math

import numpy as np
import pytest

from telemetry_nerd.analysis.littles import Substeps, _systematic, check, combine, judge

from .littles_sim import simulate, substeps

LOAD = [(0.0, 2.0), (1200.0, 3.7), (2400.0, 2.0)]  # M/M/4, mu=1: rho 0.5 -> 0.925 -> 0.5


def test_consistent_mmc_false_alarms_at_most_nominal():
    """Timer from arrival: L = lambda W holds; the overall verdict (5% by construction) must not
    alarm more often than that, and pointwise 95% intervals must cover 1 at least 95% of the time."""
    n, alarms, covered, windows = 60, 0, 0, 0
    for seed in range(n):
        r = check(substeps(simulate(seed, rates=[(0.0, 2.0)])), k=20)
        alarms += r.verdict != "consistent"
        for w in r.windows:
            windows += 1
            covered += w.ci95[0] <= 1 <= w.ci95[1]
    # binomial(60, 0.05): P(>= 7 alarms) < 2%; conservative intervals keep it far lower
    assert alarms <= 6
    assert covered / windows >= 0.95


def test_consistent_under_heavy_load_and_high_concurrency():
    alarms = 0
    for seed in range(20):
        r = check(substeps(simulate(100 + seed, rates=[(0.0, 19.0)], c=25)), k=20)
        alarms += r.verdict != "consistent"
    assert alarms <= 2


@pytest.mark.parametrize("seed", range(5))
def test_unmeasured_queueing_is_detected_and_localised_in_time(seed):
    """The timer starts at service start: queueing is in L, not in W. Only the loaded third
    (20-40 min) queues materially; flagged windows must lie there (plus one window of drain)."""
    r = check(substeps(simulate(seed, rates=LOAD, timer="service_start")), k=20)
    assert r.verdict in ("L_high", "inconsistent_in_windows")
    assert r.flagged, "some loaded window flagged"
    assert all(r.windows[i].verdict == "L_high" for i in r.flagged)
    assert all(1200_000 <= r.windows[i].start_ms < 2700_000 for i in r.flagged)
    assert all(w.verdict == "consistent" for w in r.windows[:4])


def test_queueing_measured_by_the_timer_is_consistent():
    r = check(substeps(simulate(3, rates=LOAD, timer="arrival")), k=20)
    assert r.verdict == "consistent"


def test_missing_instance_makes_the_total_low():
    sims = [simulate(10 + i, rates=[(0.0, 6.0)], c=10) for i in range(3)]
    parts = [substeps(m, drop_gauge=(i == 2)) for i, m in enumerate(sims)]
    total = check(combine(parts), k=20)
    assert total.verdict == "L_low"
    assert total.pooled.ci95[1] < 0.8  # about 2/3 of lambda W is in the gauge
    whole = check(combine([substeps(m) for m in sims]), k=20)
    assert whole.verdict == "consistent"


def test_leaked_requests_grow():
    m = simulate(7, rates=[(0.0, 4.0)], c=8)
    m["gauge"] = m["gauge"] + np.floor(m["ts"] / 120)  # one stuck request every 2 minutes
    r = check(substeps(m), k=20)
    assert r.verdict == "L_high"
    assert r.growing and r.growing["growing"]


def test_no_traffic_is_not_judged():
    ts = np.arange(1, 41, dtype=np.int64) * 15_000
    z = np.zeros(40)
    sub = Substeps(ts, z, z, z, np.ones(40), np.ones(40), 15_000)
    b = judge(sub, np.arange(40))
    assert b.verdict == "no_traffic"
    assert "in_flight_without_traffic" in b.flags


def test_too_few_substeps_is_insufficient():
    m = simulate(1, duration_s=60)
    assert judge(substeps(m), np.arange(3)).verdict == "insufficient"


def test_constant_gauge_still_gets_the_sampling_floor():
    """A gauge that reads the same at every scrape (spikes in between) must not give a zero-width
    interval: the Poisson-occupancy floor applies."""
    ts = np.arange(1, 41, dtype=np.int64) * 15_000
    lam = np.full(40, 2.0)
    sub = Substeps(ts, lam, lam * 0.5, lam, np.full(40, 1.0), np.ones(40), 15_000)
    b = judge(sub, np.arange(40))
    assert b.sd_source == "floor"
    assert b.ci95[1] - b.ci95[0] > 0.2


def test_systematic_sampling_factor_limits():
    assert _systematic(1000.0, 1.0) == 1.0  # independent samples
    assert math.isclose(_systematic(1e-4, 1.0), 1e-4 / 6)
    assert 0 < _systematic(1.0, 1.0) < 1


def test_flow_balance_flags_a_counter_counting_more():
    m = simulate(4, rates=[(0.0, 5.0)], c=10)
    sub = substeps(m)
    doubled = Substeps(
        sub.ts_ms, sub.arrivals * 1.5, sub.lat_sum, sub.lat_count, sub.conc, sub.conc_n,
        sub.step_ms,
    )  # fmt: skip
    r = check(doubled, k=20)
    assert "flow_imbalance" in r.pooled.flags
    assert "flow_imbalance" not in check(sub, k=20).pooled.flags
