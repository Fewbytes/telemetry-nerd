"""Missing-data claims about public instances, pinned to recorded fixtures (bead 1h9.10).

Each test names the question it answers in docs/data-source-quirks.md. Fixtures were recorded
politely from the public registry sources by scripts/record_missing_data.py; nothing here
touches the network.
"""

from __future__ import annotations

import itertools
from collections import Counter

from tests.unit.missing_data_fx import fill_after, fx, matrix, only, raw_samples

T = "thanos"
M = "mimir"
V = "victoriametrics"


def _counts(fid: str) -> list[float]:
    return [v for _, v in only(matrix(fid))]


# --- Thanos (TQ1-TQ5) ---------------------------------------------------------------------------


def test_tq1_default_datasource_serves_raw_even_on_downsample_datasources():
    # [1h] windows at 1m native resolution hold ~60 samples on all three datasources
    for src in ("wikimedia-raw", "wikimedia-1h", "wikimedia-5m"):
        counts = _counts(f"{T}/{src}__{src}_count_over_time_default")
        assert all(55 <= c <= 65 for c in counts), src
    for src in ("wikimedia-1h", "wikimedia-5m"):
        samples = raw_samples(f"{T}/{src}__{src}_samples_default")
        spacing = sorted(b - a for (a, _), (b, _) in itertools.pairwise(samples))
        assert spacing[len(spacing) // 2] == 60


def test_tq1_tier_is_chosen_by_max_source_resolution_param_and_not_reported():
    for tier, spacing in (("res5m", 300), ("res1h", 3600)):
        samples = raw_samples(f"{T}/wikimedia-1h__wikimedia-1h_samples_{tier}")
        gaps = sorted(b - a for (a, _), (b, _) in itertools.pairwise(samples))
        assert gaps[len(gaps) // 2] == spacing
    # `auto` with a 1 h step picked the 5 m tier
    assert set(_counts(f"{T}/wikimedia-1h__wikimedia-1h_count_over_time_resauto")) == {12.0}
    # nothing in the envelope says which resolution answered
    for claim in ("res5m", "res1h", "resauto"):
        body = fx(f"{T}/wikimedia-1h__wikimedia-1h_count_over_time_{claim}")["body"]
        assert set(body) == {"status", "data"}
        assert set(body["data"]) <= {"resultType", "result", "stats", "analysis"}


def test_tq2_count_over_time_on_a_tier_counts_downsampled_points_not_raw_samples():
    # REFUTES the belief that the raw sample count survives in the count aggregate
    assert set(_counts(f"{T}/wikimedia-1h__wikimedia-1h_count_over_time_res5m")) == {12.0}
    assert set(_counts(f"{T}/wikimedia-5m__wikimedia-5m_count_over_time_res5m")) == {12.0}
    one_hour = set(_counts(f"{T}/wikimedia-1h__wikimedia-1h_count_over_time_res1h"))
    assert one_hour <= {1.0, 2.0}


def test_tq2_downsampled_aggregates_differ_from_raw_at_window_edges():
    raw = only(matrix(f"{T}/wikimedia-raw__wikimedia-raw_max_over_time_default"))
    tier = only(matrix(f"{T}/wikimedia-1h__wikimedia-1h_max_over_time_res5m"))
    assert [t for t, _ in raw] == [t for t, _ in tier]
    diffs = [abs(a - b) for (_, a), (_, b) in zip(raw, tier)]
    assert any(d > 0 for d in diffs)  # not identical: tier block boundaries differ from raw


def test_tq4_dedup_merges_replicas_so_a_gap_in_one_replica_is_invisible():
    on = fx(f"{T}/wikimedia-raw__dedup_on")["body"]["data"]["result"][0]["value"][1]
    off = fx(f"{T}/wikimedia-raw__dedup_off")["body"]["data"]["result"][0]["value"][1]
    assert (on, off) == ("1", "2")  # one series after dedup, two replicas without it


def test_tq3_no_warnings_key_in_any_recorded_thanos_response():
    # partial responses could not be provoked on the public instances: unknown, not "absent"
    body = fx(f"{T}/wikimedia-raw__partial_response_param")["body"]
    assert "warnings" not in body


def test_tq5_metadata_with_the_same_params_returns_different_names_each_call():
    sets = [set(fx(f"{T}/wikimedia-raw__metadata_limit_{i}")["body"]["data"]) for i in range(3)]
    assert all(len(s) == 5 for s in sets)
    assert not (sets[0] & sets[1]) and not (sets[1] & sets[2])


def test_thanos_stale_marker_ends_a_scraped_series_at_the_next_scrape_not_after_lookback():
    # Wikimedia: pod series, 60 s scrape; evaluation stops 55 s after its last sample
    samples = raw_samples(f"{T}/wikimedia-raw__wm_ended_samples")
    raw = only(matrix(f"{T}/wikimedia-raw__wm_ended_raw"))
    gap = (samples[-1][0], samples[-1][0] + 900)
    assert 30 <= fill_after(raw, gap) <= 90


def test_thanos_0_32_lookback_fills_300s_inclusive_after_a_series_without_stale_marker():
    # CERN EOS (0.32.5): one-sample series, no marker: still evaluated 300 s later. The query grid
    # is aligned to the sample, so 300.0 means the lookback is INCLUSIVE (Prometheus 2.x
    # semantics); the local Prometheus 3 drops the sample at exactly 300 s (test_missing_data_local)
    samples = raw_samples(f"{T}/cern-eos__eos_ended_samples")
    raw = only(matrix(f"{T}/cern-eos__eos_ended_raw"))
    gap = (samples[-1][0], samples[-1][0] + 900)
    assert fill_after(raw, gap) == 300.0


# --- Mimir (MQ1-MQ3) ---------------------------------------------------------------------------


def test_mq1_otlp_series_linger_for_the_lookback_scraped_series_end_at_their_stale_marker():
    otlp_samples = raw_samples(f"{M}/grafana-play__play_otlp_ended_samples")
    otlp = only(matrix(f"{M}/grafana-play__play_otlp_ended_raw"))
    gap = (otlp_samples[-1][0], otlp_samples[-1][0] + 900)
    assert 270 <= fill_after(otlp, gap) < 300  # no staleness marker: whole 5 m lookback

    scr_samples = raw_samples(f"{M}/grafana-play__grafana-play_scrape_ended_samples")
    scr = only(matrix(f"{M}/grafana-play__grafana-play_scrape_ended_raw"))
    gap = (scr_samples[-1][0], scr_samples[-1][0] + 900)
    assert fill_after(scr, gap) <= 75  # marker at the next 60 s scrape


def test_mq2_no_seam_at_a_utc_midnight_split_for_aligned_steps():
    rec = fx(f"{M}/grafana-play__split_seam_up")
    p = rec["request"]["params"]
    steps = int((float(p["end"]) - float(p["start"])) // 300) + 1
    ts = [t for s in rec["body"]["data"]["result"] for t, _ in s["values"]]
    assert len(ts) == steps and len(set(ts)) == len(ts)  # no missing, no duplicate bucket


def test_mq3_limit_errors_are_400_with_a_documented_message():
    for src in ("grafana-play", "cern-openstack"):
        rec = fx(f"{M}/{src}__step_limit_over")
        assert rec["status"] == 400
        assert rec["body"]["errorType"] == "bad_data"
        assert "exceeded maximum resolution of 11,000 points" in rec["body"]["error"]


# --- cross-backend ------------------------------------------------------------------------------


def test_xq3_before_retention_is_an_empty_success_not_an_error():
    for fid in (
        f"{T}/wikimedia-raw__retention_edge",
        f"{M}/grafana-play__retention_edge",
        f"{M}/cern-openstack__retention_edge",
        f"{V}/percona-pmm__retention_edge",
        "prometheus/prometheus-demo__retention_edge",
    ):
        rec = fx(fid)
        assert rec["status"] == 200 and rec["body"]["status"] == "success"
        assert rec["body"]["data"]["result"] == [], fid


def test_xq3_some_public_instances_keep_over_two_years_so_the_edge_is_not_universal():
    for fid in (f"{T}/cern-eos__retention_edge", f"{V}/vm-playground__retention_edge"):
        assert fx(fid)["body"]["data"]["result"], fid  # data from ~900 days ago


def test_thanos_limit_error_envelope_differs_by_proxy():
    # Wikimedia's proxy returns the message as text/plain, CERN's as the JSON envelope
    wm = fx(f"{T}/wikimedia-raw__step_limit_over")
    eos = fx(f"{T}/cern-eos__step_limit_over")
    assert wm["status"] == eos["status"] == 400
    assert isinstance(wm["body"], str) and "exceeded maximum resolution" in wm["body"]
    assert "exceeded maximum resolution" in eos["body"]["error"]


def test_xq1_series_in_one_query_do_not_share_a_sample_interval():
    play = fx(f"{M}/grafana-play__xq1_samples_per_5m")["body"]["data"]["result"]
    counts = Counter(r["value"][1] for r in play)
    assert len(counts) >= 5  # push/scrape mix: 5, 10, 14, 15, 19, 20, 28... samples per 5 m
    vm = fx(f"{V}/vm-playground__xq1_samples_per_5m")["body"]["data"]["result"]
    assert {r["value"][1] for r in vm} == {"20"}  # one 15 s interval


def test_vq3_cluster_marks_responses_with_is_partial_single_node_does_not():
    cluster = fx(f"{V}/vm-playground__shape_range")["body"]
    assert cluster["isPartial"] is False
    assert "isPartial" not in fx(f"{V}/percona-pmm__shape_range")["body"]


# --- shared PromQL engine: extrapolation and two-sample rule (PQ4 on real backends) ----------------

ENGINE = [
    (T, "wikimedia-raw"),  # Thanos 0.38, 60 s scrape
    (T, "cern-eos"),  # Thanos 0.32.5, 15 s scrape
    (M, "grafana-play"),  # Mimir (weekly), 60 s scrape
    (M, "cern-openstack"),  # Mimir 2.15, 10 s scrape
]


def test_pq4_increase_extrapolates_to_window_edges_on_thanos_and_mimir():
    import pytest

    checked = 0
    for backend, src in ENGINE:
        samples = raw_samples(f"{backend}/{src}__engine_samples")
        inc = only(matrix(f"{backend}/{src}__engine_increase_2m"))
        for t, v in inc:
            inside = [x for ts, x in samples if t - 120 < ts <= t]
            if len(inside) < 2 or inside[-1] <= inside[0] or inside[-1] - inside[0] < 1e-9:
                continue
            diff = inside[-1] - inside[0]
            span = [ts for ts, _ in samples if t - 120 < ts <= t]
            covered = span[-1] - span[0]
            if covered < 1:
                continue
            # extrapolated: the answer scales the sampled diff toward the full 120 s window
            assert v == pytest.approx(diff * 120 / covered, rel=0.35) or v > diff, (src, t)
            checked += 1
    assert checked >= 20


def test_pq4_a_window_with_one_sample_has_no_increase_on_thanos_and_mimir():
    for backend, src in (
        (T, "wikimedia-raw"),
        (M, "grafana-play"),
    ):  # 60 s scrape, [30s] window holds at most one sample
        counts = _counts(f"{backend}/{src}__engine_count_30s")
        assert set(counts) == {1.0}
        assert fx(f"{backend}/{src}__engine_increase_30s")["body"]["data"]["result"] == []
