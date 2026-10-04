import numpy as np
import pytest

from telemetry_nerd.analysis.spc import DECIDING, control_chart
from telemetry_nerd.devtools.synthetic import (
    SPC_DEMO_SHIFT,
    SPC_DEMO_SPIKE,
    SPC_DEMO_STEPS,
    demo_text,
    exposition,
    periodic_buckets,
    spc_demo_text,
    spc_demo_values,
)


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
        if ln.startswith("tn_demo_holes_seconds") and f'instance="{instance}"' in ln
    ]


def test_gappy_hole_is_anchored_to_wall_clock_so_reseeding_never_fills_it():
    # Seeds overlap in the persistent dev VM: a hole placed relative to each seed's own range
    # gets filled by the next seed. Anchored holes agree across seeds, and the hole is exactly
    # [offset, offset + width): the scrape just before and the one at its end are present.
    period, offset, width, scrape = 3 * 3_600_000, 3_600_000, 15 * 60_000, 15_000
    in_hole = lambda t: offset <= t % period < offset + width
    for start in (0, 2 * 3_600_000 + 15_000, 5 * 3_600_000):
        end = start + 6 * 3_600_000
        d = set(_gappy(start, end, "d"))
        assert d and not any(in_hole(t) for t in d)
        first = start + (offset - start) % period  # first hole start at or after the window start
        holes = [h for h in range(first, end, period) if h + width < end]  # scrapes are < end
        assert holes, "every 6h window holds at least one complete hole"
        for h in holes:
            assert h + width in d  # first scrape after the hole
            assert h - scrape in d or h - scrape < start  # last scrape before it
            assert {t for t in range(h, h + width, scrape)}.isdisjoint(d)


def test_late_series_starts_mid_range():
    start, end = 0, 6 * 3_600_000
    assert min(_gappy(start, end, "e")) >= (end - start) // 2


def test_demo_text_origin_makes_the_series_a_function_of_time_since_origin():
    # the e2e fixture source anchors the demo series at its start (y7hb): two anchors give the
    # same values at the same offsets, so every run reads the same shapes relative to "now"
    a, b = 1_000_000_215_000, 1_000_007_385_000  # arbitrary, multiples of the 15s scrape
    six_h = 6 * 3_600_000

    def shifted(origin):
        out = []
        for ln in demo_text(origin - six_h, origin, origin_ms=origin).splitlines():
            head, value, ts = ln.rsplit(" ", 2)
            out.append((head, value, int(ts) - origin))
        return out

    assert shifted(a) == shifted(b)


# --- the e2e SPC series (bead ax1s) -----------------------------------------------------------


def _spc(y: np.ndarray):
    pos = np.arange(y.size)
    return control_chart(pos, pos * 60.0, y, pos < y.size // 2)  # the panel's default baseline


@pytest.mark.parametrize(("phi", "mode"), [(0.0, "individuals"), (0.7, "ar1_residuals")])
def test_spc_demo_has_an_unambiguous_special_cause_in_either_mode(phi, mode):
    """The spc spec asserts a flagged point: it must not depend on noise sitting near a limit."""
    y = np.array(spc_demo_values(phi=phi))
    c = _spc(y)
    assert c.mode == mode
    # flagged by a deciding rule (on the residuals in ar1 mode) and outside the drawn band
    assert SPC_DEMO_SPIKE in c.detectors["beyond_3sigma"].indices
    assert SPC_DEMO_SPIKE in c.outside.indices
    # twice the 3-sigma distance even at the pessimistic ends of the limits' 99% intervals
    assert (y[SPC_DEMO_SPIKE] - c.centre_interval[1]) / c.sigma_interval[1] > 6


def test_spc_demo_has_supplementary_only_flags_for_the_toggle():
    """The spec unchecks 'supplementary rules' and expects fewer marks: some flagged points must
    be flagged by run rules only."""
    c = _spc(np.array(spc_demo_values()))
    deciding = {
        i for i, rules in c.violations().items() if set(rules) & {*DECIDING, "outside_limits"}
    }
    supplementary_only = set(c.violations()) - deciding
    lo, hi = SPC_DEMO_SHIFT
    assert any(lo <= i < hi for i in supplementary_only)


def test_spc_demo_text_is_the_same_series_on_any_step_grid():
    a = spc_demo_text("m", end_ms=1_800_000_000_000)
    b = spc_demo_text("m", end_ms=1_800_000_000_000 + 37 * 60_000)
    va = [ln.split()[1] for ln in a.splitlines()]
    vb = [ln.split()[1] for ln in b.splitlines()]
    assert va == vb and len(va) == SPC_DEMO_STEPS
    ts = [int(ln.split()[2]) for ln in b.splitlines()]
    assert ts[-1] == 1_800_000_000_000 + 37 * 60_000 and all(t % 60_000 == 0 for t in ts)
    with pytest.raises(ValueError, match="whole step"):
        spc_demo_text("m", end_ms=1_800_000_000_000 + 1)
