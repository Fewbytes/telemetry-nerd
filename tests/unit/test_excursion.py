"""The excursion test: judged points against the baseline when no control chart can (bead 7f15).

Round 5's shipping-slowdown run (tests/fixtures/evals/shipping-slowdown.live-sonnet-5.*): analyze
(d6) = mean span latency by service, 2 m step, 24 points, default baseline (first half). shipping
went from ~1 ms to ~260 ms and back inside the second half, and came back insufficient_data
("n_eff 8.9 < 10: 24 points but autocorrelation time 2.7 steps"): the episode fits neither the
step nor the trend model, so its own rise and fall were the residuals' autocorrelation. Seeded
calibration: scripts/calibrate_excursion.py."""

from __future__ import annotations

import numpy as np
import pytest

from scripts.calibrate_excursion import noise
from telemetry_nerd.analysis.autocorr import n_eff, positions, tau_int
from telemetry_nerd.analysis.diagnostics import diagnose, structure
from telemetry_nerd.analysis.excursion import excursion

S = 120_000  # 2 m


def shipping(judged, seed=3):
    """15 baseline points ~1 ms (the first two the previous scenario's tail), then `judged`."""
    rng = np.random.default_rng(seed)
    base = np.r_[2.6, 1.7, 1.0 + 0.08 * rng.standard_normal(13)]
    y = np.r_[base, judged]
    return np.arange(y.size, dtype=np.int64) * S, y


EPISODE = [1.0, 1.1, 30, 140, 255, 262, 258, 200, 94]  # the ramp of a [4m] rate window, to 22:00
BACK = [1.02, 0.97, 1.05, 35, 260, 265, 255, 120, 1.3]  # the same episode, back by the end


def run(ts, y, n_base=15):
    return diagnose(ts, y, S, np.arange(y.size) < n_base, None)


def test_round5_shipping_episode_was_hidden_by_the_n_eff_gate():
    """The root cause: tau from the residuals of the best structure (here a trend), which
    still hold the episode, pushes n_eff under 10."""
    ts, y = shipping(EPISODE)
    pos = positions(ts, S)
    st = structure(pos, ts, (ts - ts[0]) / 1000, y, int(ts[-1] - ts[0]) + S)
    assert st.model != "excursion" and n_eff(y.size, tau_int(pos, st.resid)) < 10


def test_round5_shipping_episode_is_special_cause():
    ts, y = shipping(EPISODE)
    d = run(ts, y)
    ex = d.excursion
    assert ex is not None and ex.status == "special_cause" and ex.p_cautious < 1e-6
    assert ex.p < ex.p_cautious  # the baseline model is the optimistic one
    # still ~94 ms at the end of the range: not back, so a level shift (still away), not a
    # transient; never insufficient_data
    assert not ex.returned and d.verdict == "level_shifted", d.reasons
    assert d.model == "excursion" and d.n_eff >= 10  # tau re-estimated without the episode
    assert 200 < ex.mean < 265 and ex.interval[0] > 150 and abs(ex.centre - 1) < 0.1
    sp = [v for v in d.variation if v["source"] == "special_cause"]
    assert sp and "under both models" in sp[0]["finding"]
    assert "against the baseline's own variation" in sp[0]["finding"] and "cautious" in sp[0]["finding"]  # fmt: skip


def test_an_episode_that_returns_is_transient():
    ts, y = shipping(BACK)
    d = run(ts, y)
    assert d.excursion.returned and d.excursion.significant
    assert d.verdict == "transient", d.reasons
    assert d.excursion.start_ms == ts[15 + 3] or d.excursion.start_ms == ts[15 + 4]


def test_a_spike_is_undetermined_not_stable():
    """One point 15 sigma out: the baseline model (normal noise) calls it special cause, the
    cautious one (t4 tails, Bonferroni over every run) does not: undetermined, both shown."""
    rng = np.random.default_rng(0)
    for _ in range(3):
        rng.normal(size=24)
    y = rng.normal(size=24)
    y[18] += 15
    ts = np.arange(24, dtype=np.int64) * S
    d = run(ts, y, 12)
    ex = d.excursion
    assert ex.status == "undetermined" and ex.p < 0.01 <= ex.p_cautious and ex.points == 1
    assert d.verdict == "undetermined", d.reasons
    und = [v for v in d.variation if v["source"] == "undetermined"]
    assert und and "baseline model only" in und[0]["finding"]


def test_against_a_reference_baseline():
    """A separately fetched baseline: every point judged, limits from the reference only."""
    rng = np.random.default_rng(2)
    ref_ts = np.arange(15, dtype=np.int64) * S
    ref_y = 1.0 + 0.08 * rng.standard_normal(15)
    ts = ref_ts[-1] + S + np.arange(9, dtype=np.int64) * S
    y = np.array(BACK)
    ex = excursion(ts, positions(ts, S), y, np.zeros(9, bool), (positions(ref_ts, S), ref_y))
    assert ex.significant and ex.n_baseline == 15 and ex.n_judged == 9 and ex.returned


def test_no_test_without_a_baseline_spread():
    ts = np.arange(24, dtype=np.int64) * S
    y = np.r_[np.zeros(12), np.ones(12)]
    assert excursion(ts, positions(ts, S), y, np.arange(24) < 12) is None  # constant: departure's
    assert excursion(ts, positions(ts, S), y, np.arange(24) < 7) is None  # < MIN_SEGMENT


def _rates(n, kind, trials, seed, k=0.0, width=4):
    rng = np.random.default_rng(seed)
    ts = np.arange(n, dtype=np.int64) * S
    pos = positions(ts, S)
    base = np.arange(n) < n // 2
    sd = float(np.std(noise(rng, 4000, kind)))
    at = n // 2 + (n - n // 2 - width) // 2
    hits = 0
    for _ in range(trials):
        y = noise(rng, n, kind)
        y[at : at + width] += k * sd
        hits += excursion(ts, pos, y, base).p_cautious < 0.01
    return hits / trials


@pytest.mark.parametrize("n", [24, 60])
@pytest.mark.parametrize(
    "kind", ["white", "ar0.8", "window2", "t3", "lognormal1", "ar0.5-lognormal0.5"]
)
def test_false_alarms_under_no_change(n, kind):
    """Calibration (null): no change, half the series baseline, Gaussian / autocorrelated /
    rate-window / heavy-tailed / skewed noise. The cautious model, which the label rests on,
    stays at or below the nominal 1%: 2000 seeded trials each, allowing 3 binomial standard
    errors (lognormal(1), per-point skew 6, is the edge: 0.99% / 0.82% at 24 / 60 points over
    20000 trials; the baseline model goes to ~60% there: scripts/calibrate_excursion.py)."""
    trials = 2000
    assert _rates(n, kind, trials, seed=n + len(kind)) <= 0.01 + 3 * (0.0099 / trials) ** 0.5


@pytest.mark.parametrize(
    ("kind", "k", "power"),
    [("white", 30, 0.8), ("white", 100, 0.97), ("window2", 100, 0.95), ("lognormal0.5", 30, 0.85)],
)
def test_power_for_a_short_episode(kind, k, power):
    """Calibration (power): a 4-step episode of k marginal sigmas in the judged half of 24
    points (the round-5 shape) is labelled special cause at least this often (400 trials)."""
    assert _rates(24, kind, 400, seed=k, k=k) >= power


# --- the analyze op on the round-5 shape ----------------------------------------------------

D6 = (
    'sum by (service_name) (rate(traces_span_metrics_duration_milliseconds_sum{service_name!="load-'
    'generator"}[4m])) / sum by (service_name) (rate(traces_span_metrics_duration_milliseconds_'
    'count{service_name!="load-generator"}[4m]))'
)


def test_round5_analyze_reports_the_episode_as_citable_special_cause(tmp_path):
    """analyze(d6), default baseline, as the run called it: 21:14-22:14 at 2 m, data until
    "now" 22:00 (24 points); shipping's episode is a special cause under both models, cited
    through the `excursion` statistic, whose source the op recorded."""
    import asyncio

    from telemetry_nerd.model.time import parse_time
    from tests.unit.fakes import FakeSource, make_service
    from tests.unit.test_born_counters import _table

    t0, t1 = (parse_time(f"2026-10-03T{t}:00Z", 0) for t in ("21:14", "22:14"))
    now = parse_time("2026-10-03T22:00:30Z", 0)
    _, ship = shipping(EPISODE)
    rng = np.random.default_rng(9)

    class Round5(FakeSource):
        async def fetch(self, expr, rng_, step_ms):
            ts = np.arange(rng_.start_ms, min(rng_.end_ms, now) + 1, step_ms)
            i = (ts - t0) // step_ms
            y = np.where(i >= 0, ship[np.clip(i, 0, ship.size - 1)], 1.0)
            rows = [({"service_name": "shipping"}, ts, y)]
            for name, level in (("payment", 3.0), ("quote", 0.4)):
                rows.append(({"service_name": name}, ts, level * (1 + 0.05 * rng.normal(size=ts.size))))  # fmt: skip
            return _table(rows)

        fetch_values = fetch

    svc = make_service(tmp_path, source=Round5(name="default", n_series=1), clock=lambda: now)
    d = asyncio.run(svc.query(D6, start=str(t0), end=str(t1), step="120s"))["dataset"]
    out = asyncio.run(svc.analyze_profiled(d))
    assert out["baseline"]["basis"] == "first half of the range (default)"
    s = next(s for s in out["series"] if s["labels"] == {"service_name": "shipping"})
    assert s["verdict"] == "level_shifted" and s["structure"] == "excursion", s["reasons"]
    ex = s["stability"]["excursion"]
    assert ex["source"] == "special_cause" and ex["label_rests_on"] == "cautious"
    assert ex["p_cautious"] < 1e-6 and ex["ratio"] > 100 and not ex["returned"]
    assert set(ex["models"]) == {"baseline", "cautious"}
    ev = ex["evidence"]
    assert ev["name"] == "excursion" and ev["source"] == "special_cause"
    assert "two models" in ev["method"].lower() or "Two models" in ev["method"]
    bare = {k: v for k, v in ev.items() if k != "source"} | {"method": "analyze"}
    assert svc.datasets.statistic_sources(
        bare["dataset"], bare["name"], bare["method"], bare["value"]
    ) == {"special_cause"}
    # the steady services are not flagged
    for other in ("payment", "quote"):
        o = next(s for s in out["series"] if s["labels"] == {"service_name": other})
        assert o["verdict"] not in ("level_shifted", "transient", "undetermined"), o["reasons"]
