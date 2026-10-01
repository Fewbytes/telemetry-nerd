import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
from telemetry_nerd.sources.spec import SourceSpec

pytestmark = pytest.mark.integration

# 10 obs/s: 90% <= 0.1s, 9% in (0.1, 1], 1% in (1, 10]
CUMULATIVE = {"0.1": 9.0, "1": 9.9, "10": 10.0, "+Inf": 10.0}


async def test_classic_histogram_p95_with_n(vm_url, tmp_path):
    t0 = (now_ms() - 2 * 3_600_000) // 60_000 * 60_000
    text = "".join(
        exposition(
            "tn_it_lat_seconds_bucket",
            {"le": le, "job": "it"},
            [(t0 + i * 15_000, rate * 15 * i) for i in range(480)],
        )
        for le, rate in CUMULATIVE.items()
    )
    push(vm_url, text)
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    await svc.source_connect(SourceSpec(name="vm", url=vm_url, flavor="victoriametrics"))

    expr = (
        "histogram_quantile(0.95, sum by (le) (rate(tn_it_lat_seconds_bucket[$__rate_interval])))"
    )
    out = await svc.query(expr, "now-100m", "now-40m", step="1m", source="vm")
    s = out["summary"]
    assert s["representation"] == "quantile" and s["n_min"] == 200
    [row] = s["series"]
    # rate window = max(4 x 15s, 60s + 15s) = 75s -> n ~ 10/s x 75s = 750 per bucket
    assert row["meaningful_buckets"] == row["buckets"] > 0
    assert 600 <= row["n_total"] / row["buckets"] <= 900
    assert 0.1 < row["min"] <= row["max"] <= 1.0  # p95 lies in the (0.1, 1] bucket

    with pytest.raises(Exception, match="percentile"):
        await svc.query(f"avg({expr})", "now-100m", "now-40m", step="1m", source="vm")
