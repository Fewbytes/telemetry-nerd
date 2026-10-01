import pytest

from telemetry_nerd.devtools.synthetic import periodic_buckets
from telemetry_nerd.model.time import TimeRange
from tests.unit.fakes import make_service

M, DAY = 60_000, 86_400_000


def put(svc, expr, r, step, rep="bucket_agg", rng=TimeRange(0, 4 * DAY)):
    return svc.datasets.put(
        source="default", expr=expr, rng=rng, step_ms=step, resolution_ms=15_000,
        result=r, representation=rep,
    ).id  # fmt: skip


def periodic(**kw):
    return periodic_buckets(
        0, 4 * DAY, M, [(5 * M, 3, None), (DAY, 5, None)], noise=1, seed=1, **kw
    )


def test_spectrum_summary_coarsens_and_cites_evidence(tmp_path):
    svc = make_service(tmp_path)
    out = svc.spectrum(put(svc, "queue_depth", periodic(), M))
    assert out["effective_step"] == "2m" and "coarsened" in out["caveats"]  # 5761 > 4096 points
    assert out["limits"] == {"shortest": "4m", "longest": "2d"}
    peaks = out["series"][0]["peaks"]
    ev = next(p["evidence"] for p in peaks if p["significant"] and abs(p["period_s"] - 300) < 10)
    assert ev["name"] == "dominant_period" and ev["interval"][0] <= 300 <= ev["interval"][1]
    assert len(str(out)) < 2600


@pytest.mark.parametrize(
    ("expr", "rep", "match"),
    [
        ("histogram_quantile(0.99, sum(rate(x_bucket[5m])) by (le))", "quantile", "percentile"),
        ("http_requests_total", "bucket_agg", "raw counter"),
    ],
)
def test_refusals_carry_hints(tmp_path, expr, rep, match):
    svc = make_service(tmp_path)
    d = put(svc, expr, periodic(), M, rep)
    for call in (lambda: svc.spectrum(d), lambda: svc.filter(d, "lowpass", "1h", "trend")):
        with pytest.raises(ValueError, match=match) as e:
            call()
        assert "hint" in str(e.value)


def test_rate_of_counter_is_fine(tmp_path):
    svc = make_service(tmp_path)
    assert svc.spectrum(put(svc, "rate(http_requests_total[5m])", periodic(), M))["series"]


def test_filter_creates_derived_dataset_with_provenance(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, "queue_depth", periodic(), M)
    out = svc.filter(d, "lowpass", "1h", "is the daily cycle growing?")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.derived["from"] == d
    assert meta.derived["label"] == "1h low-pass (Gaussian, zero-phase)"
    assert meta.expr == svc.datasets.meta(d).expr and meta.step_ms == M
    assert out["summary"]["views"] == ["overlay", "filtered", "raw"]
    with pytest.raises(ValueError, match="already filtered"):
        svc.filter(out["dataset"], "highpass", "1h", "x")
    with pytest.raises(ValueError, match="Nyquist"):
        svc.filter(d, "lowpass", "1m", "x")
    with pytest.raises(ValueError, match="reason"):
        svc.filter(d, "lowpass", "1h", "")


async def test_mcp_spectrum_and_filter_tools(tmp_path):
    import json

    from telemetry_nerd.mcp.server import build_mcp
    from tests.unit.test_mcp import call, text_of

    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://127.0.0.1:7070")
    d = put(svc, "queue_depth", periodic(), M)
    out = json.loads(text_of(await call(mcp, "spectrum", {"dataset": d})))
    assert out["series"][0]["peaks"] and out["limits"]["shortest"]
    f = json.loads(text_of(await call(mcp, "filter", {
        "dataset": d, "kind": "lowpass", "period": "1h", "reason": "trend"})))  # fmt: skip
    assert f["dataset"] != d and f["summary"]["default_view"] == "overlay"
    q = put(
        svc, "histogram_quantile(0.9, sum(rate(x_bucket[5m])) by (le))", periodic(), M, "quantile"
    )
    bad = await call(mcp, "spectrum", {"dataset": q})
    assert bad.is_error and "hint" in text_of(bad)
