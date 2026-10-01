import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import TimeRange, now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration

# observations per second, cumulative by le: 90% <= 0.1s, 9% in (0.1, 1], 1% in (1, 10]
CUMULATIVE = {"0.1": 9.0, "1": 9.9, "10": 10.0, "+Inf": 10.0}


async def _service(vm_url, tmp_path):
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(SourceSpec(name="vm", url=vm_url, flavor="victoriametrics"))
    return svc


async def test_classic_histogram_distribution_end_to_end(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    push(vm_url, "".join(
        exposition("tn_it_dist_seconds_bucket", {"le": le, "job": "it"},
                   [(t0 + i * 15_000, rate * 15 * i) for i in range(480)])
        for le, rate in CUMULATIVE.items()
    ))  # fmt: skip
    svc = await _service(vm_url, tmp_path)
    out = await svc.query_distribution(
        'tn_it_dist_seconds_bucket{job="it"}',
        start="now-100m",
        end="now-40m",
        step="1m",
        source="vm",
    )
    s = out["summary"]
    assert s["buckets"] == "classic le buckets: 0.1, 1, 10"
    [row] = s["series"]
    assert row["missing_columns"] == 0 and row["low_n_columns"] == 0
    assert row["quantile_buckets"] == {
        "p50": ["-Inf", 0.1],
        "p90": ["-Inf", 0.1],
        "p99": [0.1, 1.0],
    }
    meta, dist = svc.datasets.get_distribution(out["dataset"])
    # VictoriaMetrics increase() uses the sample before each window: columns tile exactly
    assert {c["n"] for c in dist.columns.to_pylist()} == {600.0}
    counts = await svc.sources.get("vm").fetch_values(
        'sum(increase(tn_it_dist_seconds_bucket{job="it",le="+Inf"}[1m]))',
        TimeRange(meta.start_ms, meta.end_ms), 60_000,
    )  # fmt: skip
    assert {round(r["avg"], 6) for r in counts.buckets.to_pylist()} == {600.0}


VMRANGES = {"1.000e-01...1.136e-01": 5, "1.000e+00...1.136e+00": 1}  # increments per 15s scrape


async def test_vmrange_histogram_distribution(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    push(vm_url, "".join(
        exposition("tn_it_vm_seconds_bucket", {"vmrange": r, "job": "it"},
                   [(t0 + i * 15_000, float(inc * i)) for i in range(480)])
        for r, inc in VMRANGES.items()
    ))  # fmt: skip
    svc = await _service(vm_url, tmp_path)
    out = await svc.query_distribution(
        'tn_it_vm_seconds_bucket{job="it"}', start="now-100m", end="now-40m", step="1m", source="vm"
    )
    assert out["summary"]["buckets"].startswith("VictoriaMetrics vmrange, 18 per decade")
    _, dist = svc.datasets.get_distribution(out["dataset"])
    assert {c["n"] for c in dist.columns.to_pylist()} == {24.0}
    assert sorted({(r["bucket_lo"], r["bucket_hi"]) for r in dist.rows.to_pylist()}) == [
        (0.1, 0.1136), (1.0, 1.136)
    ]  # fmt: skip
