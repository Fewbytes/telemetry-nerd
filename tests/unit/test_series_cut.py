"""Over the line budget, show() chooses and says so instead of refusing (14y).

Shapes from the payment-failure live run (tests/fixtures/evals/payment-failure.live-sonnet-4.*):
`sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}
[1m]))` held 7 services and show() refused it with series_budget; Claude had to re-query."""

import json
import math
from pathlib import Path

import pyarrow as pa
import pytest

from telemetry_nerd.charts.series_cut import OTHERS_ID, member_label, rank, split
from telemetry_nerd.charts.spec import LINE_SERIES_BUDGET
from telemetry_nerd.core.service import ChartRejected
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.series import series_id as sid_of

from .fakes import FakeSource, make_service

FIXTURE = Path(__file__).parents[1] / "fixtures/evals/payment-failure.live-sonnet-4.stream.jsonl"
SERVICES = ["checkout", "payment", "frontend", "cart", "ad", "email", "shipping"]
ERRORING = {"checkout": 6.0, "payment": 5.0, "frontend": 4.0}  # peak error rate in the incident


def _refused_in_the_run() -> list[str]:
    out = []
    for line in FIXTURE.read_text().splitlines():
        e = json.loads(line)
        for c in e.get("message", {}).get("content", []) if e["type"] == "user" else []:
            text = c.get("content") if isinstance(c.get("content"), str) else ""
            if "series_budget" in text:
                out.append(text)
    return out


class ServicesSource(FakeSource):
    """One series per service: flat near zero, the erroring ones step up mid-range."""

    def __init__(self, services=SERVICES, label="service_name", **kw):
        super().__init__(**kw)
        self.services, self.label = services, label

    async def fetch(self, expr, rng, step_ms):
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        labels = [{self.label: s} for s in self.services]
        sids = [sid_of(self.name, lb) for lb in labels]
        mid = ts[len(ts) // 2]
        rows = [
            (t, sid, ERRORING.get(s, 0.0) if t >= mid else 0.01 * k)
            for k, (s, sid) in enumerate(zip(self.services, sids, strict=True))
            for t in ts
        ]
        n = len(rows)
        buckets = pa.table({"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
                            "avg": [r[2] for r in rows], "min": [r[2] for r in rows],
                            "max": [r[2] for r in rows], "count": [1] * n},
                           schema=BUCKET_SCHEMA)  # fmt: skip
        series = pa.table({"series_id": sids, "labels": [labels_json(lb) for lb in labels]},
                          schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)


def test_the_run_refused_line_charts_over_the_budget():
    refused = _refused_in_the_run()
    assert len(refused) == 2 and all("line charts allow at most 5" in r for r in refused)


async def test_services_over_budget_draw_top_lines_and_an_others_band(tmp_path):
    svc = make_service(tmp_path, ServicesSource())
    q = await svc.query(
        'sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ER'
        'ROR"}[1m]))', start="now-2h", end="now-1h", step="1m",
    )  # fmt: skip
    res = svc.show(q["dataset"], "Which services emit error spans, and when?")
    layer = res.panel.spec["layers"][0]
    assert layer["mark"] == "line+envelope"  # services are not members of one fleet
    top = layer["top"]
    assert len(top["keep"]) == LINE_SERIES_BUDGET - 1 and top["total"] == 7
    # the three erroring services stand out and are kept
    _, result = svc.datasets.get(q["dataset"])
    lbl = {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}
    kept = [lbl[s]["service_name"] for s in top["keep"]]
    assert set(ERRORING) <= set(kept)
    # stated, never silent: the warning names what was summarised
    (cut,) = [i for i in res.issues if i.rule == "series_cut"]
    others = [s for s in SERVICES if s not in kept]
    assert "7 series exceed the line budget (5)" in cut.message
    assert all(s in cut.message for s in others) and "'others' band" in cut.message
    # the panel: kept lines + one others row; the note on the panel says the same
    data = svc.panel_data(res.panel.id, width_px=800)
    ids = [s["id"] for s in data["series"]]
    assert ids[:-1] == top["keep"] and ids[-1] == OTHERS_ID
    band = data["series"][-1]
    assert band["summary"]["members"] == len(others) and band["summary"]["line"] == "median"
    assert set(band["reporting"]) == {len(others)}
    note = next(c for c in data["located"] if c["code"] == "series_cut")
    assert all(s in note["message"] for s in others)


async def test_members_of_one_group_switch_to_the_fleet_view(tmp_path):
    svc = make_service(tmp_path, FakeSource(n_series=8))
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    res = svc.show(ds, "Which instance is slow?")
    assert res.panel.spec["layers"][0]["mark"] == "fleet"
    (w,) = [i for i in res.issues if i.rule == "series_budget_fleet"]
    assert "8 series" in w.message and "instance" in w.message and f"fleet({ds})" in w.message
    assert svc.panel_data(res.panel.id, width_px=800)["kind"] == "fleet"


async def test_explicit_lines_over_budget_cut_instead_of_fleet(tmp_path):
    svc = make_service(tmp_path, FakeSource(n_series=8))
    ds = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    res = svc.show(ds, "Which instance is slow?", mark="line+envelope")
    assert res.panel.spec["layers"][0]["top"]["total"] == 8
    assert [i.rule for i in res.issues if i.rule.startswith("series")] == ["series_cut"]


async def test_percentiles_are_never_pooled_into_a_band(tmp_path):
    src = FakeSource(n_series=7, values={"histogram_count": 250.0, "histogram_quantile": 0.4})
    svc = make_service(tmp_path, src)
    q = "histogram_quantile(0.95, sum by (le, instance) (rate(lat_bucket[5m])))"
    ds = (await svc.query(q, "now-2h", "now-1h", step="1m"))["dataset"]
    with pytest.raises(ChartRejected) as exc:
        svc.show(ds, "Which instance is slow?")
    assert exc.value.issues[0].rule == "series_budget"


def test_member_label_needs_one_metric_and_member_labels_only():
    pods = [{"pod": f"p{k}", "service": "checkout"} for k in range(6)]
    assert member_label(pods) == "pod"
    assert member_label([{"service_name": s} for s in SERVICES]) is None
    assert member_label([{**p, "__name__": f"m{k}"} for k, p in enumerate(pods)]) is None
    assert member_label([{"pod": "a", "code": "500"}, {"pod": "b", "code": "200"}]) is None


def test_rank_puts_outstanding_series_first_and_empty_ones_last():
    rows = [(t, s, v) for t in (1, 2, 3) for s, v in (("a", 1.0), ("b", 1.1), ("c", 9.0))]
    rows.append((1, "d", math.nan))
    t = pa.table({"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
                  "avg": [r[2] for r in rows]})  # fmt: skip
    assert rank(t, ["a", "b", "c", "d"]) == ["c", "a", "b", "d"]


def test_split_summarises_every_other_series():
    mk = lambda i, vals: {"id": i, "labels": {}, "ts": [1, 2], "avg": vals, "min": vals,
                          "max": vals, "count": [1, 1]}  # fmt: skip
    rows = [mk("a", [5.0, 6.0]), mk("b", [1.0, None]), mk("c", [3.0, 2.0]), mk("d", [2.0, 4.0])]
    out = split(rows, ["a"], ["b", "c", "d"])
    assert [r["id"] for r in out] == ["a", OTHERS_ID]
    o = out[1]
    assert o["avg"] == [2.0, 3.0] and o["min"] == [1.0, 2.0] and o["max"] == [3.0, 4.0]
    assert o["reporting"] == [3, 2] and o["summary"]["members"] == 3
