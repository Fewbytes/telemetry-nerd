import numpy as np

from telemetry_nerd.analysis.spectrum import fap, level, lomb_scargle, spectrogram, spectrum
from telemetry_nerd.devtools.synthetic import periodic_buckets

DAY, M = 86_400_000, 60_000


def arrays(r):
    return np.array(r.buckets.column("ts_ms").to_pylist()), np.array(
        r.buckets.column("avg").to_pylist()
    )


def near(peaks, period_ms, tol):
    return [p for p in peaks if abs(p.period_ms - period_ms) <= tol]


def test_recovers_5m_and_24h_above_significance():
    ts, y = arrays(
        periodic_buckets(0, 4 * DAY, 2 * M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=1)
    )
    sp = spectrum(ts, y, 2 * M, top=5)
    assert (sp.shortest_ms, sp.longest_ms) == (4 * M, 2 * DAY)
    five, day = near(sp.peaks, 5 * M, 10_000), near(sp.peaks, DAY, 6 * 3_600_000)
    assert five and five[0].significant and five[0].lo_ms <= 5 * M <= five[0].hi_ms
    assert day and day[0].significant and day[0].lo_ms <= DAY <= day[0].hi_ms
    assert day[0].hi_ms - day[0].lo_ms >= DAY / 8  # resolution floor: 4 cycles cannot be sharp


def test_gappy_input_without_interpolation():
    r = periodic_buckets(
        0,
        4 * DAY,
        2 * M,
        [(5 * M, 3, None), (DAY, 5, None)],
        noise=1,
        seed=2,
        gaps=[(DAY, DAY + 6 * 3_600_000), (3 * DAY, 3 * DAY + 3_600_000)],
    )
    ts, y = arrays(r)
    sp = spectrum(ts, y, 2 * M)
    assert near(sp.peaks, 5 * M, 10_000)[0].significant and near(sp.peaks, DAY, 6 * 3_600_000)


def test_white_noise_false_alarms_are_calibrated():
    rng = np.random.default_rng(3)
    t = np.arange(300) * 60_000
    alarms = sum(
        spectrum(t, rng.normal(size=300), 60_000, top=1).peaks[0].fap < 0.05 for _ in range(100)
    )
    assert alarms <= 12  # nominal 5


def test_level_inverts_fap():
    t = np.arange(500.0)
    z = level(0.01, 500, 0.5, t)
    assert abs(float(fap(z, 500, 0.5, t)) - 0.01) < 1e-4


def test_power_is_share_of_variance():
    t = np.arange(1000.0)
    p = lomb_scargle(t, np.sin(2 * np.pi * t / 50), np.array([1 / 50, 1 / 7]))
    assert p[0] > 0.99 and p[1] < 0.01


def test_spectrogram_shows_2m_oscillation_appearing_mid_range():
    r = periodic_buckets(0, 12 * 3_600_000, 15_000, [(2 * M, 3, 6 * 3_600_000)], noise=1, seed=4)
    ts, y = arrays(r)
    sg = spectrogram(ts, y, 15_000, segment_ms=30 * M, overlap=0.5)
    k = int(np.argmin(abs(1 / sg.freqs - 120)))  # row nearest a 2m period
    before = sg.power[sg.centres_ms + 15 * M <= 6 * 3_600_000, k]
    after = sg.power[sg.centres_ms - 15 * M >= 6 * 3_600_000, k]
    assert before.max() < 0.1 and after.min() > 0.5
    assert (sg.segment_ms, sg.hop_ms) == (30 * M, 15 * M)


# lkn.4: 'significant' is confirmed against AR(1) red noise -----------------------------
def _ar1(n, phi, seed):
    rng = np.random.default_rng(seed)
    e = rng.normal(size=n)
    x = np.empty(n)
    x[0] = e[0] / np.sqrt(1 - phi * phi)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def test_ar1_wandering_is_not_significant():
    t = np.arange(1440) * M
    sig = [
        p
        for s in range(50)
        for p in spectrum(t, _ar1(1440, 0.7, s), M, top=8).peaks
        if p.significant
    ]
    assert len(sig) <= 2  # nominal 1% per series; white-noise FAP alone flags long periods
    white = sum(
        any(p.white_significant for p in spectrum(t, _ar1(1440, 0.7, s), M, top=8).peaks)
        for s in range(50)
    )
    assert white >= len(sig)


def test_step_is_not_a_significant_long_period():
    t = np.arange(1440)
    flagged = 0
    for s in range(20):
        y = np.random.default_rng(s).normal(size=1440) + np.where(t >= 1000, 2.0, 0.0)
        sp = spectrum(t * M, y, M, top=8)
        flagged += any(p.significant for p in sp.peaks)
        assert all(0 <= p.fap_red_noise <= 1 for p in sp.peaks)
    assert flagged <= 2


def test_true_cycle_on_red_noise_stays_significant():
    t = np.arange(1440)
    for s in range(10):
        y = 2 * np.sin(2 * np.pi * t / 120) + _ar1(1440, 0.5, s)
        sp = spectrum(t * M, y, M, top=8)
        hit = near(sp.peaks, 120 * M, 5 * M)
        assert hit and hit[0].significant and hit[0].fap_red_noise < 0.01
