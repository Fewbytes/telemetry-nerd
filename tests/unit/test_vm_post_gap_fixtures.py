"""Which VictoriaMetrics rollups carry a gap into the first buckets after it (bead
telemetry-nerd-mj0, docs/data-source-quirks.md VQ2). Fixtures: scripts/record_vm_post_gap.py,
VM v1.137.0, sample every 15 s with a hole from +600 s to +1200 s, step 15 s, windows 15 s and 75 s;
counter +15 per sample, gauge 100 + 2i ramp."""

from __future__ import annotations

import re

import pytest

from telemetry_nerd.model.companions import post_gap_buckets, previous_sample_windows
from tests.unit.missing_data_fx import fx, matrix, only

V = "victoriametrics"
STEP_S = 15
AFTER = 1200  # first sample after the hole, seconds after t0


def load(kind: str, func: str, w: int) -> tuple[dict[int, float], str]:
    fid = f"{V}/vm__pg_{kind}_{func}_w{w}"
    rec = fx(fid)
    t0 = int(re.search(r"t0=(\d+)", rec["note"]).group(1))
    pts = {int(t) - t0: v for t, v in only(matrix(fid))}
    return pts, rec["request"]["params"]["query"]


def spiked_buckets(pts: dict[int, float], w: int) -> int:
    """Buckets, contiguous from the first sample after the hole, whose value differs from the
    steady state (the value 20 steps later) by more than 20 %; absent buckets stop the count."""
    steady = pts[AFTER + 20 * STEP_S]
    n = 0
    while (t := AFTER + n * STEP_S) in pts and abs(pts[t] - steady) > 0.2 * abs(steady):
        n += 1
    return n


AFFECTED = ["increase", "increase_pure", "delta", "idelta"]


@pytest.mark.parametrize("kind", ["counter", "gauge"])
@pytest.mark.parametrize("w", [15, 75])
@pytest.mark.parametrize("func", AFFECTED)
def test_flagged_functions_spike_exactly_as_many_buckets_as_we_flag(kind, w, func):
    pts, query = load(kind, func, w)
    assert spiked_buckets(pts, w) == post_gap_buckets(query, STEP_S * 1000) >= 1


@pytest.mark.parametrize("w", [15, 75])
def test_increase_and_delta_cover_the_whole_gap_for_a_window_of_buckets(w):
    for func in ("increase", "increase_pure", "delta"):
        pts, _ = load("counter", func, w)
        assert pts[AFTER] == 615  # 41 samples' worth, not the window's 15
    assert post_gap_buckets(f"delta(x[{w}s])", 15_000) == w // 15


@pytest.mark.parametrize("w", [15, 75])
def test_idelta_returns_the_raw_sample_for_one_bucket_whatever_the_window(w):
    c, query = load("counter", "idelta", w)
    assert c[AFTER] == 1215 and c[AFTER + 15] == 15  # the sample itself: no previous to subtract
    g, _ = load("gauge", "idelta", w)
    assert g[AFTER] == 260  # absolute value, not a +2 change
    assert post_gap_buckets(query, 15_000) == 1


@pytest.mark.parametrize("kind", ["counter", "gauge"])
@pytest.mark.parametrize("w", [15, 75])
@pytest.mark.parametrize("func", ["rate", "irate"])
def test_rate_and_irate_leave_the_first_bucket_after_the_gap_empty_not_spiked(kind, w, func):
    pts, query = load(kind, func, w)
    steady = pts[AFTER + 20 * STEP_S]
    assert AFTER not in pts  # no value rather than a spike
    assert all(v == pytest.approx(steady) for t, v in pts.items() if t >= AFTER)
    assert post_gap_buckets(query, 15_000) == 0 and previous_sample_windows(query) == []


@pytest.mark.parametrize("w", [15, 75])
def test_rate_over_sum_and_deriv_ignore_the_sample_before_the_gap(w):
    pts, query = load("counter", "rate_over_sum", w)
    assert pts[AFTER] == pytest.approx(1215 / w)  # the window's own sample (+ none before)
    d, dq = load("counter", "deriv", w)
    assert d[AFTER] == 0  # one sample in the window: no slope, not a spike
    assert max(d[t] for t in d if t >= AFTER) <= 1.0 + 1e-9
    for q in (query, dq):
        assert post_gap_buckets(q, 15_000) == 0


@pytest.mark.parametrize("func", ["increase", "delta"])
def test_window_starting_inside_the_hole_still_computes_from_the_pre_window_sample(func):
    pts = only(matrix(f"{V}/vm__pg_straddle_{func}_w15"))
    assert pts[0][1] == 615  # the very first bucket: previous sample is before the query window
