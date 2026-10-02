"""fleet end to end over stored datasets: summary, refusals, churn, panel (bead lkn.3)."""

import json

import numpy as np
import pyarrow as pa
import pytest

from telemetry_nerd.core.service import ChartRejected
from telemetry_nerd.model.series import (
    BUCKET_SCHEMA,
    SERIES_SCHEMA,
    FetchResult,
    labels_json,
    series_id,
)
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.workspace.models import StatisticRef
from tests.unit.fakes import make_service
from tests.unit.fleet_sim import fleet

STEP = 300_000
T0 = 1_000 * STEP


def fetch_result(y: np.ndarray, labels: list[dict]) -> FetchResult:
    m, t = y.shape
    ts = T0 + np.arange(t, dtype=np.int64) * STEP
    cols: dict[str, list] = {
        "ts_ms": [],
        "series_id": [],
        "avg": [],
        "min": [],
        "max": [],
        "count": [],
    }
    for i in range(m):
        ok = np.isfinite(y[i])
        sid = series_id("default", labels[i])
        cols["ts_ms"] += ts[ok].tolist()
        cols["series_id"] += [sid] * int(ok.sum())
        for k in ("avg", "min", "max"):
            cols[k] += y[i][ok].tolist()
        cols["count"] += [20] * int(ok.sum())
    buckets = pa.table(cols, schema=BUCKET_SCHEMA)
    series = pa.table(
        {
            "series_id": [series_id("default", lb) for lb in labels],
            "labels": [labels_json(lb) for lb in labels],
        },
        schema=SERIES_SCHEMA,
    )
    return FetchResult(buckets, series)


def put(svc, y, labels=None, expr="rate(node_cpu_seconds_total[5m])", rep="bucket_agg"):
    m, t = y.shape
    labels = labels or [{"pod": f"api-{i:03d}", "job": "api"} for i in range(m)]
    return svc.datasets.put(
        source="default", expr=expr, rng=TimeRange(T0, T0 + (t - 1) * STEP), step_ms=STEP,
        resolution_ms=15_000, result=fetch_result(y, labels), representation=rep,
    ).id  # fmt: skip


def test_fleet_names_planted_members_with_labels_kind_and_evidence(tmp_path):
    svc = make_service(tmp_path)
    y, planted = fleet(11, m=100, plant=True)
    d = put(svc, y)
    out = svc.fleet(d)
    assert out["members"]["count"] == 100 and out["members"]["tested"] == 100
    assert out["scale"].startswith("log")
    found = {o["member"]: o for o in out["outliers"]}
    assert set(found) == {f"pod=api-{i:03d}" for i in planted.values()}, list(found)
    pers = found[f"pod=api-{planted['persistent']:03d}"]
    assert pers["kind"] == "persistent" and pers["labels"]["pod"] == "api-007"
    assert pers["since_window_start"] and 1.5 < pers["offset"]["ratio"] < 2.2
    assert found[f"pod=api-{planted['transient']:03d}"]["kind"] == "transient"
    assert found[f"pod=api-{planted['transient']:03d}"]["episodes"][0]["sustained"]
    assert found[f"pod=api-{planted['drifting']:03d}"]["kind"] in ("drifting", "shifted")
    for o in out["outliers"]:
        StatisticRef.model_validate(o["evidence"])  # ready for finding_create
    assert out["tests"]["family_wise_alpha"] == 0.01 and not out["tests"]["leave_one_out"]
    assert out["draw"] == f'show("{d}", question, mark="fleet")'
    assert len(json.dumps(out)) < 9000  # compact for Claude: 100 members, no per-step arrays


def test_churn_gaps_and_honest_n_per_step(tmp_path):
    svc = make_service(tmp_path)
    y, _ = fleet(12, m=40)
    y[5, 200:] = np.nan  # stops reporting two-thirds in
    y[9, :120] = np.nan  # appears later
    y[np.random.default_rng(0).random(y.shape) < 0.08] = np.nan
    out = svc.fleet(put(svc, y))
    churn = out["churn"]
    assert [c["member"] for c in churn["stopped_reporting"]] == ["pod=api-005"]
    assert [c["member"] for c in churn["appeared"]] == ["pod=api-009"]
    assert "cannot tell" in churn["note"]
    cov = out["coverage"]
    assert cov["n_per_step"]["max"] <= 40 and cov["n_per_step"]["min"] < 36
    assert 0.08 < cov["missing_share"] < 0.15 and "members_missing" in out["caveats"]


@pytest.mark.parametrize(
    ("expr", "rep", "labels"),
    [
        ("histogram_quantile(0.99, sum by (le, pod) (rate(x_bucket[5m])))", "bucket_agg", None),
        ("x_latency_seconds", "quantile", None),
        ("rpc_duration_seconds", "bucket_agg",
         [{"pod": f"p{i}", "quantile": "0.99"} for i in range(12)]),
    ],
)  # fmt: skip
def test_percentile_series_are_refused(tmp_path, expr, rep, labels):
    svc = make_service(tmp_path)
    d = put(svc, fleet(13, m=12)[0], labels=labels, expr=expr, rep=rep)
    with pytest.raises(ValueError, match="fleet refused on percentile series"):
        svc.fleet(d)


def test_member_identity_units_and_size_preconditions(tmp_path):
    svc = make_service(tmp_path)
    y = fleet(14, m=12)[0]
    shared = [{"pod": f"p{i // 2}", "container": f"c{i % 2}"} for i in range(12)]
    d = put(svc, y, labels=shared)
    with pytest.raises(ValueError, match=r"does not identify members.*sum by \(pod\)"):
        svc.fleet(d, by=["pod"])
    with pytest.raises(ValueError, match="no member has label"):
        svc.fleet(d, by=["node"])
    assert svc.fleet(d, by=["pod", "container"])["members"]["count"] == 12
    mixed = [{"__name__": "a_seconds" if i < 6 else "b_bytes", "pod": f"p{i}"} for i in range(12)]
    with pytest.raises(ValueError, match="must share a unit"):
        svc.fleet(put(svc, y, labels=mixed, expr='{__name__=~"a_seconds|b_bytes"}'))
    with pytest.raises(ValueError, match="a fleet needs >= 5 members"):
        svc.fleet(put(svc, y[:3]))


def test_long_ranges_are_coarsened_per_member_first(tmp_path):
    svc = make_service(tmp_path)
    y = fleet(15, m=12, t=2000)[0]
    out = svc.fleet(put(svc, y))
    assert "coarsened" in out["caveats"] and out["effective_step"] == "10m"


def test_show_fleet_panel_draws_band_n_and_at_most_six_outliers(tmp_path):
    svc = make_service(tmp_path)
    y, planted = fleet(16, m=100, plant=True)
    d = put(svc, y)
    with pytest.raises(ChartRejected) as exc:
        svc.show(d, "CPU of all api pods")  # 100 lines: refused, with the group hint
    assert "fleet(dataset)" in exc.value.issues[0].message
    svc.fleet(d, normalise="none")
    shown = svc.show(d, "CPU of all api pods: who is off?", mark="fleet")
    data = svc.panel_data(shown.panel.id, 800)
    assert data["kind"] == "fleet" and data["members"] == 100
    assert len(data["ts"]) == 288 and len(data["band"]["median"]) == 288
    assert len(data["n"]) == 288 and max(data["n"]) == 100
    assert {o["id"] for o in data["outliers"]} == {f"pod=api-{i:03d}" for i in planted.values()}
    assert len(data["outliers"]) <= 6 and data["outlier_count"] == 3
    assert all(len(o["values"]) == 288 for o in data["outliers"])


async def test_mcp_fleet_tool_and_refusal(tmp_path):
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp
    from tests.unit.test_mcp import text_of

    svc = make_service(tmp_path)
    d = put(svc, fleet(17, m=20)[0])
    q = put(svc, fleet(17, m=20)[0], expr="x_seconds", rep="quantile")
    async with Client(build_mcp(svc, "http://x")) as client:
        ok = await client.call_tool("fleet", {"dataset": d, "by": ["pod"]})
        assert not ok.is_error
        assert json.loads(text_of(ok))["members"]["named_by"] == ["pod"]
        bad = await client.call_tool("fleet", {"dataset": q})
        assert bad.is_error and "percentile" in text_of(bad)
        shown = await client.call_tool("show", {"dataset": d, "question": "Pods?", "mark": "fleet"})
        assert not shown.is_error
