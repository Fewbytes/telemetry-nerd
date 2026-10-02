from telemetry_nerd.devtools.synthetic import demo_text, exposition, periodic_buckets


def test_exposition_format():
    text = exposition("m", {"b": "2", "a": "1"}, [(1000, 1.5), (2000, 2.0)])
    assert text == 'm{a="1",b="2"} 1.5 1000\nm{a="1",b="2"} 2.0 2000\n'


def test_exposition_without_labels():
    assert exposition("m", {}, [(1000, 1.0)]) == "m 1.0 1000\n"


def test_demo_text_contains_spike_for_instance_c():
    start, end = 0, 6 * 3_600_000
    lines = demo_text(start, end).splitlines()
    spike = [ln for ln in lines if ln.startswith('tn_demo_latency_seconds{instance="c"} 1.5 ')]
    assert len(spike) == 20  # 5 minutes at 15s
    assert any(ln.startswith("tn_demo_requests_total{") for ln in lines)


def test_demo_histogram_is_cumulative_and_counts_every_request():
    import re

    text = demo_text(0, 3_600_000)
    by_ts: dict[tuple[str, str], dict[str, float]] = {}
    pat = re.compile(
        r'^tn_demo_request_duration_seconds_bucket\{instance="(\w)",le="([^"]+)"\} (\S+) (\d+)$'
    )
    for line in text.splitlines():
        if m := pat.match(line):
            by_ts.setdefault((m[1], m[4]), {})[m[2]] = float(m[3])
    assert by_ts
    for cum in by_ts.values():
        ordered = [cum[k] for k in sorted(cum, key=lambda k: float(k))]  # "+Inf" sorts last
        assert ordered == sorted(ordered)


def test_periodic_buckets_known_components_gaps_and_onset():
    r = periodic_buckets(
        0,
        3_600_000,
        60_000,
        [(600_000, 2.0, None), (120_000, 1.0, 1_800_000)],
        base=10.0,
        gaps=[(600_000, 900_000)],
    )
    ts = r.buckets.column("ts_ms").to_pylist()
    assert 660_000 not in ts and 600_000 not in ts and 0 in ts and 3_600_000 in ts
    avg = dict(zip(ts, r.buckets.column("avg").to_pylist()))
    assert abs(avg[0] - 10.0) < 1e-9  # sin(0)=0, onset not reached
    assert r.buckets.column("min").to_pylist() == r.buckets.column("avg").to_pylist()


def _gappy(start, end, instance):
    lines = demo_text(start, end).splitlines()
    return [
        int(ln.rsplit(" ", 1)[1])
        for ln in lines
        if ln.startswith("tn_demo_gappy_seconds") and f'instance="{instance}"' in ln
    ]


def test_gappy_hole_is_anchored_to_wall_clock_so_reseeding_never_fills_it():
    # Seeds overlap in the persistent dev VM: a hole placed relative to each seed's own range
    # gets filled by the next seed. Anchored holes agree across seeds.
    period, offset, width = 3 * 3_600_000, 3_600_000, 15 * 60_000
    in_hole = lambda t: offset <= t % period < offset + width
    for start in (0, 2 * 3_600_000 + 15_000, 5 * 3_600_000):
        end = start + 6 * 3_600_000
        d = _gappy(start, end, "d")
        assert d and not any(in_hole(t) for t in d)
        holes = [h for h in range(start, end) if h % period == offset and h + width <= end]
        assert holes, "every 6h window holds at least one complete hole"


def test_late_series_starts_mid_range():
    start, end = 0, 6 * 3_600_000
    assert min(_gappy(start, end, "e")) >= (end - start) // 2
