import numpy as np
import pytest

from telemetry_nerd.analysis.filters import FilterSpec, apply, filter_buckets, segments
from telemetry_nerd.devtools.synthetic import periodic_buckets

M = 60_000
t = np.arange(0, 2000) * M


def amp(spec, period_ms):
    f = apply(spec, t, np.sin(2 * np.pi * t / period_ms), M)
    return np.abs(f.values[~f.edge]).max()


def test_lowpass_half_power_at_cutoff_and_passes_long_periods():
    lp = FilterSpec("lowpass", 30 * M)
    assert amp(lp, 120 * M) > 0.95 and amp(lp, int(7.5 * M)) < 0.01
    assert abs(amp(lp, 30 * M) - 2**-0.5) < 0.03


def test_highpass_removes_long_periods():
    hp = FilterSpec("highpass", 30 * M)
    assert amp(hp, 120 * M) < 0.1 and amp(hp, int(7.5 * M)) > 0.95
    assert abs(amp(hp, 30 * M) - 2**-0.5) < 0.03


def test_bandpass_isolates_band():
    bp = FilterSpec("bandpass", 10 * M, 120 * M)
    assert amp(bp, 30 * M) > 0.8 and amp(bp, 3 * M) < 0.05 and amp(bp, 600 * M) < 0.1


def test_gap_splits_and_nothing_crosses_it():
    ts = np.r_[np.arange(0, 100), np.arange(150, 250)] * M
    y = np.r_[np.zeros(100), np.full(100, 10.0)]
    assert segments(ts, M) == [(0, 100), (100, 200)]
    f = apply(FilterSpec("lowpass", 20 * M), ts, y, M)
    assert np.allclose(f.values[:100], 0) and np.allclose(
        f.values[100:], 10
    )  # no bleeding, no ramp
    assert f.edge[0] and f.edge[99] and f.edge[100] and not f.edge[50]


def test_checks_nyquist_range_and_band_shape():
    with pytest.raises(ValueError, match="Nyquist"):
        FilterSpec("lowpass", M).check(M, 100 * M)
    with pytest.raises(ValueError, match="half the range"):
        FilterSpec("lowpass", 60 * M).check(M, 100 * M)
    with pytest.raises(ValueError, match="4 x"):
        FilterSpec("bandpass", 10 * M, 20 * M).check(M, 1000 * M)
    assert FilterSpec("lowpass", 3 * M).check(M, 100 * M) == ["weak_filter"]


def test_filter_buckets_keeps_series_ids_and_reports_edges():
    r = periodic_buckets(
        0, 600 * M, M, [(5 * M, 1, None), (200 * M, 3, None)], gaps=[(300 * M, 310 * M)]
    )
    out = filter_buckets(FilterSpec("lowpass", 20 * M), r.buckets, M)
    assert (
        out.buckets.column("series_id").unique().to_pylist()
        == r.buckets.column("series_id").unique().to_pylist()
    )
    sid = r.buckets.column("series_id")[0].as_py()
    # the gap's two edge zones touch and merge: start, around the gap, end
    assert len(out.edges[sid]) == 3 and 0 < out.removed_share[sid] < 0.2
