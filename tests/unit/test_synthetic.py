from telemetry_nerd.devtools.synthetic import demo_text, exposition


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
