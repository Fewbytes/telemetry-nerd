import json

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.workspace.models import StatisticRef
from tests.unit.fakes import make_service

M = 60_000
N = 1440


def buckets(y, labels=None, keep=None):
    lb = labels or {"job": "x"}
    ts = np.arange(len(y), dtype=np.int64) * M
    if keep is not None:
        ts, y = ts[keep], np.asarray(y)[keep]
    sid = "s-" + "-".join(lb.values())
    n = len(ts)
    t = pa.table(
        {"ts_ms": ts, "series_id": [sid] * n, "avg": y, "min": y, "max": y, "count": [1] * n},
        schema=BUCKET_SCHEMA,
    )
    return FetchResult(t, pa.table({"series_id": [sid], "labels": [labels_json(lb)]}, schema=SERIES_SCHEMA))  # fmt: skip


def put(svc, y, expr="queue_depth", rep="bucket_agg", **kw):
    return svc.datasets.put(
        source="default", expr=expr, rng=TimeRange(0, (N - 1) * M), step_ms=M,
        resolution_ms=15_000, result=buckets(y, **kw), representation=rep,
    ).id  # fmt: skip


def step_series(seed=1):
    t = np.arange(N)
    return np.random.default_rng(seed).normal(size=N) + 10 + np.where(t >= 1000, 2.0, 0.0)


def test_analyze_step_with_default_baseline_and_evidence(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, step_series())
    out = svc.analyze(d)
    assert out["baseline"] == {
        "start": "1970-01-01T00:00:00+00:00", "end": "1970-01-01T12:00:00+00:00",
        "basis": "first half of the range (default)",
    }  # fmt: skip
    (s,) = out["series"]
    assert s["verdict"] == "level_shifted" and s["n"] == N
    shift = s["stability"]["shifts"][0]
    assert (
        shift["at"].startswith("1970-01-01T16:4")
        and shift["interval"][0] < 2 < shift["interval"][1]
    )
    spc = s["spc"]
    assert spc["in_control"] is False and spc["baseline"]["n"] == 720
    assert spc["first_violations"][0]["t"] >= "1970-01-01T12:00"  # judged points only
    assert spc["detectors"]["cusum"]["p"] < 1e-6 and spc["detectors"]["cusum"]["expected"] < 2
    for e in (shift["evidence"], spc["centre"]["evidence"], spc["sigma"]["evidence"]):
        StatisticRef.model_validate(e)  # citable as-is
    assert len(json.dumps(out)) < 6000


def test_stated_baseline_is_validated_and_used(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, step_series())
    out = svc.analyze(d, "1970-01-01T02:00:00Z", "1970-01-01T08:00:00Z")
    assert out["baseline"]["basis"] == "stated" and out["series"][0]["spc"]["baseline"]["n"] == 360
    with pytest.raises(ValueError, match="inside the dataset range"):
        svc.analyze(d, "1970-01-03T00:00:00Z", "1970-01-04T00:00:00Z")


@pytest.mark.parametrize(
    ("expr", "rep", "match"),
    [
        ("histogram_quantile(0.99, sum(rate(x_bucket[5m])) by (le))", "quantile", "percentile"),
        ("http_requests_total", "bucket_agg", "raw counter"),
    ],
)
def test_analyze_refuses_with_hints(tmp_path, expr, rep, match):
    svc = make_service(tmp_path)
    d = put(svc, step_series(), expr=expr, rep=rep)
    with pytest.raises(ValueError, match=match) as e:
        svc.analyze(d)
    assert "hint" in str(e.value) and "analyze" in str(e.value)


def test_too_few_points_is_an_honest_verdict_not_an_error(tmp_path):
    svc = make_service(tmp_path)
    keep = np.zeros(N, bool)
    keep[:20] = True
    out = svc.analyze(put(svc, step_series(), keep=keep))
    (s,) = out["series"]
    assert s["verdict"] == "insufficient_data" and "too_few_points" in s["reasons"][0]


def test_spc_panel_payload(tmp_path):
    svc = make_service(tmp_path)
    d = put(svc, step_series())
    p = svc.show(d, "Did the queue depth shift?", mark="spc").panel
    assert p.spec["layers"][0]["mark"] == "spc" and p.spec["layers"][0]["windows"] == []
    data = svc.panel_data(p.id, 800)
    assert data["kind"] == "spc" and data["baseline"]["end_ms"] == 720 * M
    (s,) = data["series"]
    assert len(s["ts"]) == len(s["value"]) == len(s["centre"]) == N
    assert s["mode"] in ("individuals", "ar1_residuals") and s["sigma"] > 0
    assert s["violations"] and all(v["ts"] >= 720 * M for v in s["violations"])


def test_show_spc_with_stated_baseline(tmp_path):
    from telemetry_nerd.charts.spec import Window

    svc = make_service(tmp_path)
    d = put(svc, step_series())
    w = Window(start_ms=60 * M, end_ms=600 * M, label="calm")
    p = svc.show(d, "Shift vs the calm morning?", mark="spc", windows=[w]).panel
    assert p.spec["layers"][0]["windows"][0]["label"] == "baseline"
    assert svc.panel_data(p.id, 800)["baseline"]["start_ms"] == 60 * M
    with pytest.raises(ValueError, match="at most one window"):
        svc.show(d, "?", mark="spc", windows=[w, w])


async def test_mcp_analyze_tool(tmp_path):
    from telemetry_nerd.mcp.server import build_mcp
    from tests.unit.test_mcp import call, text_of

    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://127.0.0.1:7070")
    d = put(svc, step_series())
    out = json.loads(text_of(await call(mcp, "analyze", {"dataset": d})))
    assert out["series"][0]["verdict"] == "level_shifted" and "spc" in out["draw"]
    bad = await call(mcp, "analyze", {"dataset": d, "baseline_start": "1970-01-05T00:00:00Z"})
    assert bad.is_error and "hint" in text_of(bad)
