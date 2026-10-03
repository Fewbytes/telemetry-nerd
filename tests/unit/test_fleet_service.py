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


def fetch_result(
    y: np.ndarray, labels: list[dict], counts: np.ndarray | None = None, failed=()
) -> FetchResult:
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
        cols["count"] += [20] * int(ok.sum()) if counts is None else counts[i][ok].tolist()
    buckets = pa.table(cols, schema=BUCKET_SCHEMA)
    series = pa.table(
        {
            "series_id": [series_id("default", lb) for lb in labels],
            "labels": [labels_json(lb) for lb in labels],
        },
        schema=SERIES_SCHEMA,
    )
    return FetchResult(buckets, series, failed=tuple(failed))


def put(
    svc, y, labels=None, expr="rate(node_cpu_seconds_total[5m])", rep="bucket_agg",
    counts=None, failed=(),
):  # fmt: skip
    m, t = y.shape
    labels = labels or [{"pod": f"api-{i:03d}", "job": "api"} for i in range(m)]
    return svc.datasets.put(
        source="default", expr=expr, rng=TimeRange(T0, T0 + (t - 1) * STEP), step_ms=STEP,
        resolution_ms=15_000, result=fetch_result(y, labels, counts, failed), representation=rep,
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
    # spec §5.4: the fleet's spread is the common-cause envelope; outliers are special causes
    assert out["spread"]["source"] == "common_cause"
    for o in out["outliers"]:
        assert o["source"] == o["evidence"]["source"] == "special_cause"
    srcs = {v["source"] for v in out["variation"]}
    assert srcs == {"common_cause", "special_cause"}


def test_homogeneous_fleet_is_common_cause_only(tmp_path):
    svc = make_service(tmp_path)
    y, _ = fleet(11, m=60)
    out = svc.fleet(put(svc, y))
    assert out["outliers"] == []
    assert {v["source"] for v in out["variation"]} == {"common_cause"}


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
    # churn and missing members are the measurement system's; a silent member is undetermined
    assert churn["appeared"][0]["source"] == "measurement_system"
    assert churn["stopped_reporting"][0]["source"] == "undetermined"
    meas = [v for v in out["variation"] if v["source"] == "measurement_system"]
    assert any(v.get("caveat") == "members_missing" for v in meas)


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


def test_heterogeneous_fleet_is_split_into_explained_groups(tmp_path):
    """lkn.10: two instance sizes (30/70): groups, the label that explains them, outliers
    named against their own group, per-group bands in the panel."""
    svc = make_service(tmp_path)
    y, planted = fleet(18, m=100, plant=True)
    y[:30] *= 2.0
    labels = [{"pod": f"api-{i:03d}", "size": "small" if i < 30 else "large"} for i in range(100)]
    d = put(svc, y, labels=labels)
    out = svc.fleet(d, by=["pod"])
    cl = out["clusters"]
    assert cl["k"] == 2 and sorted(g["size"] for g in cl["groups"]) == [30, 70]
    assert cl["explained_by"][0]["label"] == "size"
    assert cl["split_tests"][0]["accepted"] and cl["split_tests"][0]["p"] < 0.01
    small = next(g for g in cl["groups"] if g["size"] == 30)
    assert small["label_values"] == {"size": ["small"]}
    assert 1.8 < small["level_vs_fleet"]["ratio"] < 2.2
    assert "clustered" in out["caveats"] and "many_outliers" not in out["caveats"]
    assert any("systemic structure" in v["finding"] and v["source"] == "common_cause"
               for v in out["variation"])  # fmt: skip
    found = {o["member"]: o for o in out["outliers"]}
    assert set(found) == {f"pod=api-{i:03d}" for i in planted.values()}
    assert found[f"pod=api-{planted['persistent']:03d}"]["cluster"] == small["id"]
    assert found[f"pod=api-{planted['drifting']:03d}"]["cluster"] != small["id"]
    for o in out["outliers"]:
        StatisticRef.model_validate(o["evidence"])
    data = svc.panel_data(svc.show(d, "CPU by size: who is off?", mark="fleet").panel.id, 800)
    assert data["outlier_count"] == 3 and all("cluster" in o for o in data["outliers"])
    assert {o["source"] for o in data["outliers"]} == {"special_cause"}
    assert [c["size"] for c in data["clusters"]] == [70, 30]
    assert len(data["clusters"][0]["band"]["median"]) == 288


def test_unknown_spans_leave_n_and_alive_and_are_located(tmp_path):
    """lkn.13: a failed fetch chunk is bucket_state UNKNOWN: neither reporting nor missing (not
    `members_missing`), never a churn edge, and located as untrusted_data with its spans."""
    svc = make_service(tmp_path)
    y, _ = fleet(19, m=40)
    y[:, 100:130] = np.nan  # the chunk that failed: nothing came back
    failed = [(T0 + 100 * STEP, T0 + 129 * STEP, "TimeoutError: chunk 2")]
    out = svc.fleet(put(svc, y, failed=failed))
    assert out["coverage"]["missing_share"] == 0.0 and "members_missing" not in out["caveats"]
    assert out["coverage"]["n_per_step"]["min"] == 0  # n drops, alive drops with it
    assert out["churn"]["stopped_reporting"] == [] and out["churn"]["appeared"] == []
    assert "untrusted_data" in out["caveats"]
    assert {"source": "measurement_system", "caveat": "untrusted_data"}.items() <= next(
        v for v in out["variation"] if v.get("caveat") == "untrusted_data"
    ).items()
    (unk,) = [c for c in out["located"] if c["code"] == "untrusted_data"]
    assert "TimeoutError" in unk["message"] and unk["where"]["series"] is None
    assert [tuple(x) for x in unk["where"]["spans"]] == [(T0 + 99 * STEP, T0 + 129 * STEP)]
    data = svc.panel_data(svc.show(put(svc, y, failed=failed), "cpu", mark="fleet").panel.id, 800)
    assert max(data["alive"][100:130]) == 0 and data["alive"][99] == 40
    assert any(c["code"] == "untrusted_data" for c in data["located"])
    # the same gap without a failed fetch is missing data (members alive, silent)
    plain = svc.fleet(put(svc, y))
    assert "members_missing" in plain["caveats"] and "untrusted_data" not in plain["caveats"]
    (miss,) = [c for c in plain["located"] if c["code"] == "members_missing"]
    assert [tuple(x) for x in miss["where"]["spans"]] == [(T0 + 99 * STEP, T0 + 129 * STEP)]


def test_partial_buckets_are_flagged_on_members_and_episodes(tmp_path):
    svc = make_service(tmp_path)
    y, planted = fleet(20, m=60, plant=True)
    counts = np.full(y.shape, 20)
    tr = planted["transient"]
    counts[tr, 144:150] = 8  # the transient's episode rests on half-empty buckets
    counts[3, :40] = 10
    out = svc.fleet(put(svc, y, counts=counts))
    (part,) = [c for c in out["located"] if c["code"] == "members_partial"]
    assert part["severity"] == "info" and len(part["where"]["series"]) == 2
    assert "members_partial" not in out["caveats"]  # under 5% of member-steps: info only
    o = next(o for o in out["outliers"] if o["member"] == f"pod=api-{tr:03d}")
    assert o["partial_buckets"] == 6 and o["episode_partial_buckets"][0] == 6
    # the excursion rests on half-empty buckets: special cause or measurement, not guessed
    assert o["source"] == "undetermined"
    assert o["evidence"]["source"] == "undetermined"
    heavy = counts.copy()
    heavy[:, :40] = 10  # every member, 14% of the window
    out = svc.fleet(put(svc, y, counts=heavy))
    assert "members_partial" in out["caveats"]


def test_stopped_members_are_silent_unless_marked_stale(tmp_path):
    svc = make_service(tmp_path)
    y, _ = fleet(21, m=30)
    y[4, 200:] = np.nan
    out = svc.fleet(put(svc, y))
    (stop,) = out["churn"]["stopped_reporting"]
    assert stop["state"] == "silent" and "cannot tell" in out["churn"]["note"]


def test_a_staleness_marker_tells_an_ended_member_from_a_silent_one(tmp_path, monkeypatch):
    """No adapter sets STALE_MARKER yet (range queries do not carry markers); when one does,
    the flag on a stopped member's buckets makes it `ended`, not `silent`."""
    import dataclasses

    import polars as pl

    from telemetry_nerd.core import fleet_ops
    from telemetry_nerd.model.bucket_state import STATE_SCHEMA, Flag

    real = fleet_ops.dataset_bundle

    def marked(store, meta, result):
        b = real(store, meta, result)
        st = pl.from_arrow(b.companions["bucket_state"])
        sid = series_id("default", {"pod": "api-004", "job": "api"})
        st = st.with_columns(
            pl.when((pl.col("series_id") == sid) & (pl.col("ts_ms") == T0 + 200 * STEP))
            .then(int(Flag.STALE_MARKER)).otherwise(pl.col("flags")).cast(pl.UInt16).alias("flags")
        )  # fmt: skip
        return dataclasses.replace(b, companions={"bucket_state": st.to_arrow().cast(STATE_SCHEMA)})

    monkeypatch.setattr(fleet_ops, "dataset_bundle", marked)
    svc = make_service(tmp_path)
    y, _ = fleet(21, m=30)
    y[4, 200:] = np.nan
    y[9, 220:] = np.nan
    out = svc.fleet(put(svc, y))
    states = {c["member"]: c["state"] for c in out["churn"]["stopped_reporting"]}
    assert states == {"pod=api-004": "ended", "pod=api-009": "silent"}
    srcs = {c["member"]: c["source"] for c in out["churn"]["stopped_reporting"]}
    assert srcs == {"pod=api-004": "measurement_system", "pod=api-009": "undetermined"}


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


def _util_fleet(svc, m=6, **kw):
    rng = np.random.default_rng(3)
    y = 0.002 + 0.0015 * rng.random((m, 60))
    expr = '1 - rate(node_cpu_seconds_total{mode="idle"}[5m])'
    d = put(svc, y, labels=[{"cpu": str(i), "instance": "n1"} for i in range(m)], expr=expr, **kw)
    return d


async def test_fleet_panel_of_a_bounded_derived_expression_gets_natural_bounds(tmp_path):
    from telemetry_nerd.charts.spec import ChartSpec

    svc = make_service(tmp_path)
    shown = svc.show(_util_fleet(svc), "per-core utilisation?", mark="fleet")
    ctx = await svc.y_context(shown.panel.id)
    assert (ctx.natural_lo, ctx.natural_hi, ctx.bounds, ctx.bounds_origin) == (
        0.0,
        1.0,
        "[0,1]",
        "rule",
    )
    y = ChartSpec.model_validate(svc.workspace.get_panel(shown.panel.id).spec).y
    assert y.context == ctx and y.unit == "ratio"
    assert ctx.lines == [] and ctx.profile is None  # per-series context does not apply


async def test_fleet_accepts_y_view_suggestions_and_selection(tmp_path):
    from telemetry_nerd.charts.yview import YView

    svc = make_service(tmp_path)
    pid = svc.show(_util_fleet(svc), "per-core utilisation?", mark="fleet").panel.id
    await svc.y_context(pid)
    v = YView(mode="data", label="data range", reason="show the spread itself", author="claude")
    saved, _ = svc.ws.suggest_y_view(pid, v, "claude")
    assert saved.id == "v1"
    svc.ws.select_y_view(pid, "user", mode="semantic")
    with pytest.raises(ValueError, match="fleet"):
        svc.ws.select_y_view(pid, "user", mode="indexed", baseline="window")


async def test_normalised_fleet_has_no_natural_bounds(tmp_path):
    svc = make_service(tmp_path)
    d = _util_fleet(svc)
    svc.fleet(d, normalise="member")
    pid = svc.show(d, "who is off?", mark="fleet").panel.id
    ctx = await svc.y_context(pid)
    assert ctx.natural_lo is None and ctx.bounds is None


async def test_asserted_bounds_where_they_cannot_apply_warn_instead_of_storing(tmp_path):
    svc = make_service(tmp_path)
    d = _util_fleet(svc, m=1)
    res = svc.show(d, "spectrum?", mark="spectrum", bounds_lo=0, bounds_hi=1)
    assert "bounds_not_applied" in [i.rule for i in res.issues]
    assert res.panel.spec["y"]["asserted_bounds"] is None


async def test_fleet_payload_carries_what_the_ui_reads_for_the_bounded_range(tmp_path):
    svc = make_service(tmp_path)
    d = _util_fleet(svc)
    pid = svc.show(d, "per-core utilisation?", mark="fleet").panel.id
    await svc.y_context(pid)
    data = svc.panel_data(pid, 800)
    ctx = data["panel"]["spec"]["y"]["context"]
    assert (ctx["natural_lo"], ctx["natural_hi"], ctx["bounds"], ctx["bounds_origin"]) == (
        0.0,
        1.0,
        "[0,1]",
        "rule",
    )
    assert ctx["bounds_basis"] and ctx["bounds_confidence"] is not None
    # what fleetY reads: the analysis scale (log for all-positive data, not the drawn axis) and normalise
    assert data["normalise"] == "none" and data["scale"] in ("log", "linear")
    assert len(data["band"]["lo"]) == len(data["ts"]) and len(data["band"]["hi"]) == len(data["ts"])


async def test_fleet_panels_refuse_a_log_y_view(tmp_path):
    svc = make_service(tmp_path)
    pid = svc.show(_util_fleet(svc), "q?", mark="fleet").panel.id
    with pytest.raises(ValueError, match="fleet"):
        svc.ws.select_y_view(pid, "user", mode="log")
