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
