import pytest

from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import TimeRange, now_ms
from telemetry_nerd.sources.promql import PromQLSource

pytestmark = pytest.mark.integration


def _hour_aligned_t0() -> int:
    return (now_ms() - 3 * 3_600_000) // 3_600_000 * 3_600_000


async def test_rollup_preserves_peak(vm_url):
    t0 = _hour_aligned_t0()
    samples = [(t0 + i * 15_000, 1000.0 if i == 42 else float(i % 4)) for i in range(240)]
    push(vm_url, exposition("tn_it_gauge", {"instance": "a"}, samples))

    src = PromQLSource("vm", vm_url)
    res = await src.fetch(
        'tn_it_gauge{instance="a"}', TimeRange(t0 + 60_000, t0 + 3_540_000), 60_000
    )
    rows = res.buckets.to_pylist()

    # sample 42 is at t0+630s; the bucket covering (600s, 660s] ends at t0+660s
    spike = next(r for r in rows if r["ts_ms"] == t0 + 660_000)
    assert spike["max"] == 1000.0
    assert spike["avg"] < 1000.0
    # 4 samples per 60s bucket; VM may include the sample on the window edge
    assert all(4 <= r["count"] <= 5 for r in rows)
    assert res.series.num_rows == 1


async def test_expression_uses_subquery(vm_url):
    t0 = _hour_aligned_t0()
    for inst in ("a", "b"):
        push(
            vm_url,
            exposition(
                "tn_it_sum", {"instance": inst}, [(t0 + i * 15_000, 1.0) for i in range(240)]
            ),
        )
    src = PromQLSource("vm", vm_url)
    res = await src.fetch("sum(tn_it_sum)", TimeRange(t0 + 120_000, t0 + 3_000_000), 60_000)
    assert res.series.num_rows == 1
    assert {r["avg"] for r in res.buckets.to_pylist()} == {2.0}
