"""Acceptance: synthetic series with known structure get the right verdict (seeded)."""

import numpy as np
import pytest

from telemetry_nerd.analysis.diagnostics import departure_from_zero, diagnose
from telemetry_nerd.analysis.spectrum import spectrum
from tests.unit.test_autocorr import ar1_series

M = 60_000
N = 1440  # one day at 1m


def run(y, step=M, base_share=0.5, gaps=None):
    ts = np.arange(y.size, dtype=np.int64) * step
    if gaps is not None:
        ts, y = ts[gaps], y[gaps]
    base = ts < ts[0] + base_share * (ts[-1] - ts[0] + step)
    try:
        sp = spectrum(ts, y, step, top=8)
    except ValueError:
        sp = None
    return diagnose(ts, y, step, base, sp)


def noise(seed, n=N):
    return np.random.default_rng(seed).normal(size=n)


@pytest.mark.parametrize("seed", range(5))
def test_white_noise_is_stable(seed):
    d = run(noise(seed))
    assert d.verdict == "stable", d.reasons
    assert d.chart.in_control and d.chart.mode in ("individuals", "ar1_residuals")


@pytest.mark.parametrize("seed", range(5))
def test_ar1_noise_is_not_called_a_drift_or_shift(seed):
    d = run(ar1_series(N, 0.7, seed))
    assert d.verdict in ("stable", "noisy"), d.reasons
    assert "drifting" not in d.also and "level_shifted" not in d.also
    assert d.chart.mode == "ar1_residuals" and d.tau > 3


@pytest.mark.parametrize("seed", range(5))
def test_known_period(seed):
    t = np.arange(N)
    d = run(3 * np.sin(2 * np.pi * t / 60) + noise(seed))
    assert d.verdict == "periodic", d.reasons
    assert abs(d.peaks[0].period_ms - 3_600_000) < 60_000
    assert d.chart.seasonal_periods_s == [pytest.approx(3600, rel=0.02)] and d.chart.in_control


@pytest.mark.parametrize("seed", range(5))
def test_step_change(seed):
    t = np.arange(N)
    d = run(noise(seed) + np.where(t >= 1000, 2.0, 0.0))
    assert d.verdict == "level_shifted", d.reasons
    (s,) = d.shifts
    assert abs(s.index - 1000) <= 5 and s.interval[0] < 2 < s.interval[1]
    assert d.chart.in_control is False


@pytest.mark.parametrize("seed", range(5))
def test_drift(seed):
    t = np.arange(N)
    d = run(noise(seed) + 3 * t / N)
    assert d.verdict == "drifting", d.reasons
    assert d.trend.change_interval[0] < 3 < d.trend.change_interval[1]


def test_period_and_step_together():
    t = np.arange(N)
    d = run(3 * np.sin(2 * np.pi * t / 60) + noise(1) + np.where(t >= 1000, 3.0, 0.0))
    assert d.verdict == "level_shifted" and "periodic" in d.also, d.reasons


def test_insufficient_data():
    d = run(noise(0, 15))
    assert d.verdict == "insufficient_data" and d.reasons == ["15 points < 16"]
    d = run(ar1_series(300, 0.995, 1))
    assert d.verdict == "insufficient_data" and "n_eff" in d.reasons[0]


def test_gaps_are_not_interpolated():
    keep = np.ones(N, bool)
    keep[300:400] = keep[900:950] = False
    t = np.arange(N)
    d = run(noise(2) + np.where(t >= 1000, 2.0, 0.0), gaps=keep)
    assert d.verdict == "level_shifted" and d.n == keep.sum()
    assert d.shifts[0].ts_ms == pytest.approx(1000 * M, abs=5 * M)


def test_heteroscedastic_noise_is_noisy():
    t = np.arange(N)
    d = run(noise(3) * np.where(t >= N // 2, 3.0, 1.0))
    assert d.verdict == "noisy", d.reasons
    assert any("variance" in r for r in d.reasons)


def test_a_short_series_is_judged_without_a_period_search():
    """< 32 points (e.g. 30 min at 1m = 31): shifts, trend and SPC are judged, periods are not
    searched (the spectrum's minimum), stated as a caveat (7thi)."""
    t = np.arange(31)
    d = run(noise(3, 31) + np.where(t >= 16, 6.0, 0.0))
    assert d.verdict == "level_shifted" and "no_period_search" in d.caveats, d.reasons
    assert not d.peaks and d.shifts[0].ts_ms == 16 * M


def test_baseline_contaminated_by_a_previous_episode_is_flagged_not_calm():
    """telemetry-nerd-k9sn: the default baseline (first half of the range) can hold the tail of
    an earlier, different episode (e.g. a previous scenario's last points still elevated). The
    excursion test is robust to it (median/MAD), but nothing said the baseline wasn't actually
    calm; it should flag `baseline_not_calm`."""
    y = 10 + noise(5)
    y[:30] += 15  # a different episode's tail bleeding into the first 30 of 720 baseline points
    d = run(y)
    assert "baseline_not_calm" in d.caveats


def test_a_calm_baseline_is_not_flagged():
    d = run(noise(6))
    assert "baseline_not_calm" not in d.caveats


def test_a_shift_only_the_point_model_sees_is_undetermined_not_level_shifted():
    """Principle 16 (nbz): a step significant under the bias-corrected AR(1) phi but not under
    the cautious one (phi + 1 SE) is reported as undetermined context, never the step model."""
    t = np.arange(64)
    d = run(ar1_series(64, 0.6, 1) + np.where(t >= 32, 2.5, 0.0))
    assert d.verdict == "undetermined" and "level_shifted" not in d.also and not d.shifts
    assert "noisy" not in d.also, d.reasons  # the shift explains the chart: not noise
    (u,) = d.shifts_undetermined
    assert u.index == 32 and u.p < 0.01 <= u.p_cautious
    (r,) = [r for r in d.reasons if "point AR(1) model only" in r]
    assert any(v["source"] == "undetermined" and v["finding"] == r for v in d.variation)


# --- departure from an all-zero baseline (event counts) ---------------------------------------

BURST = np.r_[np.zeros(14), [9.0, 14, 12, 13, 13], np.zeros(3)]  # round 4 payment: 61 errors


def zero_base(y, n_base=13, scale=1.0, sibling=None):
    ts = np.arange(y.size, dtype=np.int64) * M
    return diagnose(ts, y, M, np.arange(y.size) < n_base, None, events_scale=scale, sibling=sibling)


def test_round4_burst_is_undetermined_under_clustered_events():
    """Round-4 payment shape (0vg7): 61 errors in a 5-step burst that ends inside the judged
    window, after 13 zero steps. Poisson p = (9/22)^61 ~ 2e-24; allowing clustered errors with
    the judged steps' own dispersion (the only error counts there are) the burst is ~4.7
    independent clusters and p = (9/22)^4.7 ~ 0.015 >= 0.01: undetermined, the Poisson p shown
    as context, never labelled a level shift."""
    d = zero_base(BURST)
    dep = d.departure
    assert dep is not None and dep.events == 61
    assert (dep.n_baseline, dep.n_judged, dep.index) == (13, 9, 14)
    assert dep.p == pytest.approx((9 / 22) ** 61)
    assert dep.dispersion_source == "judged" and dep.dispersion_sibling is None
    assert dep.clusters == pytest.approx(61 / dep.dispersion)
    assert dep.p_clustered == pytest.approx((9 / 22) ** dep.clusters)
    assert 0.01 < dep.p_clustered < 0.02 and dep.p < 1e-20
    assert dep.status == "undetermined" and not dep.significant
    assert dep.mean == pytest.approx(61 / 9) and dep.interval[0] < dep.mean < dep.interval[1]
    assert "level_shifted" not in [d.verdict, *d.also] and d.verdict != "stable", d.reasons
    und = [v for v in d.variation if v["source"] == "undetermined" and "departure" in v["finding"]]
    assert und and "under a Poisson model" in und[0]["finding"]
    assert "allowing clustered events" in und[0]["finding"]
    assert not [v for v in d.variation if v["source"] == "special_cause"]


def test_a_longer_zero_baseline_decides_the_round4_burst():
    """The same burst after 40 zero steps instead of 13: D is the judged steps' own (unchanged),
    but ~4.7 clusters all landing in the last 9 of 49 steps is now unlikely under no change."""
    y = np.r_[np.zeros(41), BURST[14:]]
    dep = zero_base(y, n_base=40).departure
    assert dep.dispersion_source == "judged" and dep.n_baseline == 40
    assert dep.p_clustered == pytest.approx((9 / 49) ** dep.clusters)
    assert dep.status == "special_cause"


def test_round4_burst_with_a_quiet_sibling_stays_undetermined():
    """A steady live sibling (requests, ~13/step, under-dispersed) shows no traffic burstiness:
    it only raises D, never lowers it below the errors' own: errors can cluster beyond their
    traffic (retries), so the label still rests on the judged steps' D."""
    ts = np.arange(BURST.size, dtype=np.int64) * M
    sib = 13.0 + np.random.default_rng(4).normal(scale=1.5, size=ts.size).round()
    d = zero_base(BURST, sibling=(ts, sib))
    dep = d.departure
    assert dep.dispersion_sibling is not None and dep.dispersion_sibling < 1
    assert dep.dispersion_source == "judged" and dep.status == "undetermined"


def test_a_poisson_burst_after_a_zero_baseline_departs_under_both_models():
    """A clean departure: from step 16 on, Poisson(5) events every step, sibling traffic
    Poisson too. The judged steps' own dispersion is ~1, so both models agree: special cause,
    a level shift, both p values stated."""
    rng = np.random.default_rng(7)
    y = np.r_[np.zeros(16), rng.poisson(5.0, 16).astype(float)]
    ts = np.arange(y.size, dtype=np.int64) * M
    sib = rng.poisson(40.0, y.size).astype(float)
    d = zero_base(y, n_base=16, sibling=(ts, sib))
    dep = d.departure
    assert dep.dispersion < 2.5 and dep.dispersion_sibling is not None
    assert dep.p_clustered < 1e-6 and dep.status == "special_cause" and dep.significant
    assert "level_shifted" in [d.verdict, *d.also], d.reasons
    sp = [v for v in d.variation if v["source"] == "special_cause" and "departure" in v["finding"]]
    assert sp and "both models" in sp[0]["finding"] and "p=" in sp[0]["finding"]


def test_a_bursty_sibling_raises_the_dispersion():
    """Traffic burstier than the errors look: thinning passes it on, so D comes from the
    sibling (and is said to)."""
    rng = np.random.default_rng(3)
    y = np.r_[np.zeros(16), rng.poisson(5.0, 16).astype(float)]
    ts = np.arange(y.size, dtype=np.int64) * M
    sib = rng.negative_binomial(0.5, 0.5 / 40.5, y.size).astype(float)  # mean 40, D ~ 80
    dep = zero_base(y, n_base=16, sibling=(ts, sib)).departure
    assert dep.dispersion_source == "sibling" and dep.dispersion == dep.dispersion_sibling
    assert dep.p_clustered > dep.p


def _false_alarms(rng, mu, size, n_base, n_judged, trials):
    """Share of `trials` series of iid negative-binomial counts (mean mu, size r: variance
    mu + mu^2 / r) in which each model flags a departure from zero at 0.01."""
    p = size / (size + mu)
    ys = rng.negative_binomial(size, p, (trials, n_base + n_judged)).astype(float)
    ts = np.arange(n_base + n_judged, dtype=np.int64) * M
    base = np.arange(n_base + n_judged) < n_base
    poisson = clustered = 0
    for y in ys:
        if y[:n_base].any() or not y[n_base:].any():
            continue
        dep = departure_from_zero(ts, y, base, 1.0, step_ms=M)
        poisson += dep.p < 0.01
        clustered += dep.p_clustered < 0.01
    return poisson / trials, clustered / trials


@pytest.mark.parametrize(
    ("mu", "size", "n_base", "n_judged"),
    [(0.3, 0.1, 16, 16), (0.5, 0.2, 13, 9), (1.0, 0.1, 16, 16), (2.0, 0.05, 16, 16)],
)
def test_departure_false_alarms_under_a_clustered_null(mu, size, n_base, n_judged):
    """Calibration: no change, events clustered (iid negative-binomial steps). The Poisson
    model over-flags (above the nominal 1%); the clustered model, which the label rests on,
    stays at or below it."""
    rng = np.random.default_rng(int(mu * 100 + size * 1000 + n_base))
    pois, clus = _false_alarms(rng, mu, size, n_base, n_judged, 20_000)
    assert clus <= 0.01, (pois, clus)
    assert pois > clus


def test_departure_needs_counts_a_zero_baseline_and_enough_events():
    assert zero_base(BURST, scale=None).departure is None  # not an event count: no test
    nonzero = BURST.copy()
    nonzero[3] = 1.0
    assert zero_base(nonzero).departure is None  # the baseline saw an event: SPC's job
    assert zero_base(BURST, n_base=7).departure is None  # < MIN_SEGMENT baseline steps
    one = np.r_[np.zeros(14), [1.0], np.zeros(7)]
    d = zero_base(one)  # 1 event: (9/22)^1, one rate explains it under either model
    assert d.departure is not None and d.departure.status == "common_cause"
    assert d.departure.p == d.departure.p_clustered == pytest.approx(9 / 22)
    assert d.verdict != "level_shifted"
    assert any(v["source"] == "common_cause" and "zero baseline" in v["finding"]
               for v in d.variation)  # fmt: skip
    # rate(x[1m]) per second at 1m: x 60 events per step; 0.1/s for 5 min = 30 events
    rate = np.r_[np.zeros(14), np.full(5, 0.1), np.zeros(3)]
    assert zero_base(rate, scale=60.0).departure.events == 30
