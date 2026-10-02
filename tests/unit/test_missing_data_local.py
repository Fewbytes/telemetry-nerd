"""Missing-data claims about Prometheus and VictoriaMetrics, pinned to fixtures recorded from the
local lab (deploy/missing-data-lab, scripts/missing_data_lab.py + scripts/record_missing_data.py local).

Ground truth is known: seeded series have exact sample timestamps (`*_samples` fixtures) and the
scraped target follows `timeline.json`. Each test names the question it answers in
docs/data-source-quirks.md.
"""

from __future__ import annotations

import json
import math

import pytest

from tests.unit.missing_data_fx import FIXTURES, fill_after, fx, gaps, matrix, only, raw_samples

P = "prometheus"
V = "victoriametrics"
TIMELINE = json.loads((FIXTURES / "missing-data/local/timeline.json").read_text())
T0 = TIMELINE["t0_ms"] / 1000
DOWN = [(T0 + a, T0 + b) for a, b in TIMELINE["down"]]
VANISH = [(T0 + a, T0 + b) for a, b in TIMELINE["vanish"]]
SEED = json.loads((FIXTURES / "missing-data/local/seed.json").read_text())
S0 = SEED["start_ms"] / 1000


def _in(t: float, spans, margin: float = 0.0) -> bool:
    """t inside a span, `margin` seconds after its start (the scrape straddling the edge)."""
    return any(a + margin <= t < b for a, b in spans)


def _times(fid: str) -> list[float]:
    return [t for t, _ in only(matrix(fid))]


# --- PQ1 / VQ1: gap filling of a raw selector ------------------------------------------------------


@pytest.mark.parametrize("case", ["i15", "i60"])
def test_pq1_prometheus_fills_gaps_shorter_than_5m_and_breaks_longer_ones(case):
    samples = raw_samples(f"{P}/prom__gapfill_{case}_samples")
    evaluated = only(matrix(f"{P}/prom__gapfill_{case}_raw"))
    interval = 15 if case == "i15" else 60
    found = 0
    for gap in gaps(samples, interval):
        length = gap[1] - gap[0]
        fill = fill_after(evaluated, gap)
        if length < 300:
            assert fill >= length - 5 - 1e-6, (case, length)  # filled across the whole gap
        else:
            assert 290 <= fill < 300, (case, length, fill)  # 5 m after the last sample, no more
        found += 1
    assert found >= 5


def test_pq1_prometheus_lookback_is_left_open_a_gap_of_exactly_5m_is_not_filled():
    samples = raw_samples(f"{P}/prom__gapfill_i60_samples")
    exact = [g for g in gaps(samples, 60) if g[1] - g[0] == 300]
    assert exact, "seeded series has a gap of exactly 300 s"
    evaluated = only(matrix(f"{P}/prom__gapfill_i60_raw"))
    assert fill_after(evaluated, exact[0]) == 295  # last 5 s-step point before +300 s


def test_vq1_victoriametrics_fills_about_one_scrape_interval_not_5m():
    for case, interval, limit in (("i15", 15, 30), ("i60", 60, 75)):
        samples = raw_samples(f"{V}/vm__gapfill_{case}_samples")
        evaluated = only(matrix(f"{V}/vm__gapfill_{case}_raw"))
        for gap in gaps(samples, interval):
            assert fill_after(evaluated, gap) <= limit, (case, gap)
        # and a gap of 2+ intervals really does break the line
        long_gaps = [g for g in gaps(samples, interval) if g[1] - g[0] >= 3 * interval]
        assert long_gaps
        assert all(fill_after(evaluated, g) < g[1] - g[0] - interval for g in long_gaps)


def test_vq1_step_wider_than_the_interval_makes_vm_fill_up_to_the_step():
    samples = raw_samples(f"{V}/vm__gapfill_i60_samples")
    long_gap = max(gaps(samples, 60), key=lambda g: g[1] - g[0])
    for step in (10, 30, 60):
        fill = fill_after(only(matrix(f"{V}/vm__gapfill_i60_step{step}")), long_gap)
        assert fill <= 70, (step, fill)


# --- window functions never fill (the adapter's query shape) ------------------------------------


@pytest.mark.parametrize("backend,src", [(P, "prom"), (V, "vm")])
def test_over_time_windows_left_open_and_absent_when_empty(backend, src):
    # [15s] windows at a 15 s step over a series sampled every 15 s: exactly one sample each
    counts = [v for _, v in only(matrix(f"{backend}/{src}__count_w15"))]
    assert set(counts) == {1.0}
    samples = raw_samples(f"{backend}/{src}__gapfill_i15_samples")
    times = _times(f"{backend}/{src}__count_w15")
    assert len(times) == len(samples)  # one point per real sample, none inside gaps
    for a, b in gaps(samples, 15):
        assert not any(a < t < b for t in times)
    # [60s] windows over a 60 s series: no point for windows inside a gap
    counts60 = only(matrix(f"{backend}/{src}__count_selector_w60"))
    s60 = raw_samples(f"{backend}/{src}__gapfill_i60_samples")
    big = max(gaps(s60, 60), key=lambda g: g[1] - g[0])
    assert not any(big[0] + 60 < t < big[1] for t, _ in counts60)


def test_vm_rollup_matches_count_over_time_gaps():
    rollup = matrix(f"{V}/vm__rollup_w60")
    assert len(rollup) == 3  # min / max / avg as `rollup` label series
    counts = _times(f"{V}/vm__count_selector_w60")
    for series in rollup.values():
        assert [t for t, _ in series] == counts


# --- the adapter's expression path: subqueries fill gaps ----------------------------------------


@pytest.mark.parametrize("backend,src", [(P, "prom"), (V, "vm")])
def test_subquery_windows_fabricate_samples_inside_gaps(backend, src):
    samples = raw_samples(f"{backend}/{src}__gapfill_i60_samples")
    big = max(gaps(samples, 60), key=lambda g: g[1] - g[0])
    selector = only(matrix(f"{backend}/{src}__count_selector_w60"))
    subq = only(matrix(f"{backend}/{src}__count_subquery_w60"))
    inside = lambda series: [v for t, v in series if big[0] < t < big[1]]
    assert inside(selector) == []  # honest: nothing observed
    assert inside(subq), "the subquery path returns counts for buckets with no samples"
    assert all(v >= 1 for v in inside(subq))
    if backend == P:
        # Prometheus: 4 evaluations per 60 s bucket, filled for the whole 5 m lookback
        assert len([t for t, _ in subq if big[0] < t < big[0] + 300]) >= 4


# --- PQ2/PQ3: scrape failure, vanished series, pushed data --------------------------------------


def _live(backend: str, src: str, claim: str, spans) -> tuple[list[float], list[float]]:
    pts = only(matrix(f"{backend}/{src}__{claim}"))
    inside = [t for t, _ in pts if _in(t, spans)]
    outside = [t for t, _ in pts if not _in(t, spans)]
    return inside, outside


@pytest.mark.parametrize("backend,src", [(P, "prom"), (V, "vm")])
def test_pq2_scrape_failure_writes_stale_markers_series_ends_within_one_scrape(backend, src):
    down_a, down_b = DOWN
    pts = only(matrix(f"{backend}/{src}__scrape_gauge_raw"))
    in_a = [t for t, _ in pts if down_a[0] <= t < down_a[1]]
    in_b = [t for t, _ in pts if down_b[0] <= t < down_b[1]]
    # evaluation stops almost immediately (markers), not after 5 min lookback / adaptive window
    assert not in_a or max(in_a) - down_a[0] <= 12, (backend, "3 min gap")
    assert not in_b or max(in_b) - down_b[0] <= 12, (backend, "7 min gap")


@pytest.mark.parametrize("backend,src", [(P, "prom"), (V, "vm")])
def test_pq2_up_tells_a_down_target_from_a_vanished_series(backend, src):
    up = {t: v for t, v in only(matrix(f"{backend}/{src}__scrape_up"))}
    during_down = [v for t, v in up.items() if _in(t, DOWN, 6)]
    assert during_down and set(during_down) == {0.0}  # target down: up == 0
    during_vanish = [v for t, v in up.items() if _in(t, VANISH, 6)]
    assert during_vanish and set(during_vanish) == {1.0}  # target up, series just absent
    vanish = [t for t, _ in only(matrix(f"{backend}/{src}__scrape_vanish_raw"))]
    assert not [t for t in vanish if VANISH[0][0] + 12 <= t < VANISH[0][1]]  # marker: ends fast


@pytest.mark.parametrize("backend,src", [(P, "prom"), (V, "vm")])
def test_pq3_pushed_series_have_no_staleness_markers_gaps_fill_per_gap_fill_rule(backend, src):
    down_a, down_b = DOWN
    pts = only(matrix(f"{backend}/{src}__scrape_rw_gauge_raw"))
    in_a = [t for t, _ in pts if down_a[0] <= t < down_a[1]]
    in_b = [t for t, _ in pts if down_b[0] <= t < down_b[1]]
    if backend == P:
        # 180 s gap is filled whole; the 420 s gap is filled for the 300 s lookback only
        assert max(in_a) >= down_a[1] - 12
        assert 285 <= max(in_b) - down_b[0] <= 305
        assert not [t for t in in_b if t > down_b[0] + 305]
    else:
        # VM: adaptive window, about one push interval (5 s) plus slack, not 5 m
        assert not in_a or max(in_a) - down_a[0] <= 30
        assert not in_b or max(in_b) - down_b[0] <= 30
    # with no markers the same gap is much longer in `count_over_time` terms: honest either way
    counts = only(matrix(f"{backend}/{src}__scrape_rw_gauge_count"))
    assert not [t for t, _ in counts if down_a[0] + 12 <= t < down_a[1]]


def test_pq3_explicit_stale_marker_over_remote_write_cuts_the_series_in_prometheus():
    pts = only(matrix(f"{P}/prom__scrape_rw_stalemark_raw"))
    a, b = DOWN[0]
    assert max(t for t, _ in pts if a <= t < b) - a <= 12


def test_pq2_stale_markers_hide_from_aggregates_and_ranges_but_vm_shows_them_in_raw_samples():
    # query_range / *_over_time never carry them (count_over_time above), but a raw range vector
    # (`x[20m]`) does on VictoriaMetrics: NaN samples at the start of each outage
    prom = raw_samples(f"{P}/prom__scrape_gauge_samples")
    assert not any(math.isnan(v) for _, v in prom)
    vm = raw_samples(f"{V}/vm__scrape_gauge_samples")
    nan_ts = [t for t, v in vm if math.isnan(v)]
    assert len(nan_ts) == 3  # outage A start, outage B start, and the run's end
    assert any(DOWN[0][0] <= t < DOWN[0][0] + 12 for t in nan_ts)
    assert any(DOWN[1][0] <= t < DOWN[1][0] + 12 for t in nan_ts)


# --- PQ4: increase()/rate() edge handling --------------------------------------------------------


def _truth_increase_per_window(w: int) -> float:
    return 1.0 * w  # counter grows exactly 1/s


def test_pq4_prometheus_increase_needs_two_samples_and_extrapolates():
    assert fx(f"{P}/prom__increase_w15")["body"]["data"]["result"] == []  # 1 sample per window
    w30 = [v for _, v in only(matrix(f"{P}/prom__increase_w30"))]
    assert set(w30) <= {30.0}  # 2 samples 15 s apart, extrapolated to the full 30 s window
    w300 = dict(only(matrix(f"{P}/prom__increase_w300")))
    # window whose data ends 15 s before its right edge: extrapolation overstates (300 vs 285)
    edge = next(t for t in w300 if abs(t - (S0 + 600)) < 1)
    assert w300[edge] == 300.0


def test_pq4_prometheus_has_no_value_where_the_window_has_a_gap_and_no_spike_after():
    for w in (30, 60, 300):
        pts = only(matrix(f"{P}/prom__increase_w{w}"))
        assert max(v for _, v in pts) <= _truth_increase_per_window(w) + 1e-6  # never above truth
    w60 = dict(only(matrix(f"{P}/prom__increase_w60")))
    s60 = [t for t in w60 if S0 + 700 <= t <= S0 + 1190]
    assert not s60  # absent through the 10 min gap


def test_vq2_vm_increase_after_a_gap_carries_the_whole_gap_the_fake_spike():
    pts = dict(only(matrix(f"{V}/vm__increase_w15")))
    # counter +15 per 15 s sample; samples 40..79 missing; truth per 15 s bucket is 15
    spike = max(pts.values())
    assert spike == 615.0  # 41 samples' worth, attributed to the first bucket after the gap
    bucket = max(pts, key=pts.get)
    assert bucket == pytest.approx(S0 + 1200, abs=1)  # first sample after the gap
    # same on other windows: the post-gap value exceeds the window's true increase by far
    for w in (30, 60, 300):
        assert max(v for _, v in only(matrix(f"{V}/vm__increase_w{w}"))) > 1.5 * w
    # every other bucket is exact
    others = sorted(v for t, v in pts.items() if t != bucket)
    assert others[-1] == 15.0


def test_vq2_vm_increase_inside_a_gap_is_zero_for_the_first_steps_not_absent():
    pts = dict(only(matrix(f"{V}/vm__increase_w15")))
    zeros = [t for t, v in pts.items() if v == 0.0 and S0 + 585 <= t <= S0 + 1200]
    assert zeros, "VM reports increase 0 (not no-data) just after the last sample before a gap"
    assert max(zeros) - min(zeros) <= 30  # but only briefly


def test_pq4_vm_increase_is_exact_where_prometheus_extrapolates():
    vm = dict(only(matrix(f"{V}/vm__increase_w300")))
    edge = next(t for t in vm if abs(t - (S0 + 600)) < 1)
    assert vm[edge] == 285.0  # data ended 15 s before the right edge: exact, not extrapolated


# --- PQ5: counter resets -----------------------------------------------------------------------


def test_pq5_prometheus_resets_misses_a_reset_on_a_step_window_boundary():
    reset_t = S0 + 1500
    w15 = [t for t, v in only(matrix(f"{P}/prom__resets_w15")) if v > 0]
    assert w15 == []  # window == step: the sample before the reset is in the previous window
    w30 = [t for t, v in only(matrix(f"{P}/prom__resets_w30")) if v > 0]
    assert any(abs(t - reset_t) < 1 for t in w30)  # a window wider than the step sees it


def test_pq5_vm_resets_sees_the_boundary_reset_via_the_previous_sample():
    reset_t = S0 + 1500
    w15 = [t for t, v in only(matrix(f"{V}/vm__resets_w15")) if v > 0]
    assert len(w15) == 1 and abs(w15[0] - reset_t) < 1
    w30 = [t for t, v in only(matrix(f"{V}/vm__resets_w30")) if v > 0]
    assert len(w30) >= 1


def test_pq5_increase_handles_the_reset_without_a_negative_bucket():
    for backend, src in ((P, "prom"), (V, "vm")):
        vals = [v for _, v in only(matrix(f"{backend}/{src}__increase_w30"))]
        assert min(vals) >= 0


# --- XQ2: partial histogram buckets -------------------------------------------------------------


def test_xq2_a_missing_le_bucket_has_no_points_while_the_others_report_and_no_warning():
    for backend, src, claim in (
        (P, "prom", "hist_increase"),
        (V, "vm", "hist_increase"),
        (P, "prom", "hist_scrape_increase"),
        (V, "vm", "hist_scrape_increase"),
    ):
        body = fx(f"{backend}/{src}__{claim}")["body"]
        assert "warnings" not in body
        res = {
            float(s["metric"]["le"]): {t for t, _ in s["values"]} for s in body["data"]["result"]
        }
        assert set(res) == {1.0, 5.0, float("inf")}  # all three le series come back...
        holes = res[float("inf")] - res[1.0]
        assert len(holes) >= 2, (backend, claim)  # ...but le=1 has no points where it was absent
        assert res[5.0] == res[float("inf")]


def test_xq2_le_label_formatting_differs_by_ingestion_path():
    # a scraped `le="1"` is normalised to "1.0" by Prometheus; remote write / VM keep "1"
    scraped = fx(f"{P}/prom__hist_scrape_increase")["body"]["data"]["result"]
    assert {s["metric"]["le"] for s in scraped} == {"1.0", "5.0", "+Inf"}
    seeded = fx(f"{P}/prom__hist_increase")["body"]["data"]["result"]
    assert {s["metric"]["le"] for s in seeded} == {"1", "5", "+Inf"}


# --- PQ6: limit errors ----------------------------------------------------------------------------


def test_pq6_limit_errors_surface_as_http_errors_never_as_truncated_results():
    rec = fx(f"{P}/prom__limit_points")
    assert rec["status"] == 400 and rec["body"]["errorType"] == "bad_data"
    rec = fx(f"{P}/prom-limits__limit_max_samples")
    assert rec["status"] == 422 and rec["body"]["errorType"] == "execution"
    assert "too many samples" in rec["body"]["error"]
    for claim in ("limit_points", "limit_series"):
        rec = fx(f"{V}/vm-limits__{claim}")
        assert rec["status"] == 422 and rec["body"]["status"] == "error"
    assert fx(f"{V}/vm__limit_points_default")["status"] == 422
    assert (
        "maximum number of points is 30000" in fx(f"{V}/vm__limit_points_default")["body"]["error"]
    )


def test_pq6_a_parse_error_is_400_bad_data():
    rec = fx(f"{P}/prom__limit_parse")
    assert rec["status"] == 400 and rec["body"]["errorType"] == "bad_data"


# --- XQ3 / XQ4 -------------------------------------------------------------------------------------


def test_xq3_vm_silently_drops_samples_older_than_retention():
    body = fx(f"{V}/vm-limits__retention_write")["body"]
    lim, big = body["vm-limits"], body["vm"]
    assert lim["write_status"] == 204 and lim["readback"] == []  # accepted, then gone
    assert lim["ignored"] and not lim["ignored"][0].endswith(" 0")  # counted in a metric
    assert big["readback"], "same samples are kept with a longer retention"


def test_xq4_out_of_order_and_duplicate_timestamps():
    prom = fx(f"{P}/prom__ooo_write")["body"]
    assert prom["dup_status"] == 400 and "duplicate sample" in prom["dup_text"]
    values = [float(v) for _, v in prom["readback"][0]["values"]]
    assert values == [1.0, 2.0]  # the out-of-order sample (3.0) was dropped
    vm = fx(f"{V}/vm__ooo_write")["body"]
    assert vm["dup_status"] == 204
    vm_values = sorted(float(v) for _, v in vm["readback"][0]["values"])
    assert vm_values == [1.0, 2.0, 3.0, 9.0]  # everything is stored, duplicate ts twice


# --- VQ4: recent data ----------------------------------------------------------------------------


def test_vq4_recent_buckets_of_a_scraped_series_are_visible_at_once_on_both():
    # NOT reproduced: neither backend hid or under-counted the newest bucket (VM with default
    # -search.latencyOffset=30s and nocache=1; re-query 40 s later gave identical counts)
    for backend, src in ((P, "prom"), (V, "vm")):
        rec = fx(f"{backend}/{src}__recent_up")
        end = float(rec["request"]["params"]["end"])
        pts = only(matrix(f"{backend}/{src}__recent_up"))
        assert end - max(t for t, _ in pts) <= 5  # the newest 5 s step has a sample
