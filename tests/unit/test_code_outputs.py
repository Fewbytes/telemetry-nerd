"""show() and tier-1 ops on datasets produced by tier-2 code (bead acb).

Code outputs are made without a kernel, as in test_exchange: query an input, prepare a run
dir, write outputs through `tn`, ingest."""

from __future__ import annotations

import math

import polars as pl
import pytest

from telemetry_nerd import tn
from telemetry_nerd.core.service import ShowResult
from telemetry_nerd.exchange import fmt
from telemetry_nerd.exchange.run import RunExchange, runs_root
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.workspace.models import FindingIn, PanelRef, Scope, StatisticRef, TimeSpan

from .fakes import NOW, FakeSource, make_service

STEP = 60_000


@pytest.fixture
def src():
    return FakeSource()


@pytest.fixture
def svc(tmp_path, src):
    return make_service(tmp_path, source=src)


@pytest.fixture
def run(svc, tmp_path, monkeypatch):
    """run(node, inputs, body) -> {output name: dataset id}: body writes outputs through tn."""
    rx = RunExchange(svc.datasets, runs_root(tmp_path), unit_of=lambda m: m.unit or "s")

    def go(node: str, inputs: list[str], body) -> dict[str, str]:
        monkeypatch.setenv(fmt.ENV_RUN_DIR, str(rx.prepare_run(node, inputs)))
        body()
        res = rx.ingest_run(node, succeeded=True)
        assert res.issues == []
        return {i.name: i.dataset_id for i in res.datasets}

    return go


async def _input(svc) -> str:
    return (await svc.query("up", start="now-2h", end="now", step="1m"))["dataset"]


def _wave(n: int = 121, with_interval: bool = False) -> pl.DataFrame:
    ts = [NOW - 2 * 3_600_000 + i * STEP for i in range(n)]
    ts = [t - t % STEP for t in ts]
    avg = [10 + 3 * math.sin(2 * math.pi * i / 20) for i in range(n)]
    df = pl.DataFrame({"ts_ms": ts, "avg": avg})
    if with_interval:
        df = df.with_columns(lo=pl.col("avg") - 1, hi=pl.col("avg") + 1)
    return df


def _scope() -> Scope:
    return Scope(
        source="default", selector="up", time_range=TimeSpan(start_ms=NOW - 3_600_000, end_ms=NOW),
        step="1m", aggregation="code",
    )  # fmt: skip


# --- show + panel payload ------------------------------------------------------------------


async def test_interval_output_shows_with_declared_unit_band_and_provenance(svc, src, run):
    d = await _input(svc)
    u = {"method": "bootstrap percentile", "level": 0.9}
    out = run("c1", [d], lambda: tn.put(_wave(with_interval=True), step_ms=STEP, unit="ms",
                                        uncertainty=u, name="smooth"))  # fmt: skip
    ds = out["smooth"]
    calls = src.calls
    res: ShowResult = await svc.show_auto(ds, "Is the smoothed latency periodic?")
    assert res.panel.spec["y"]["unit"] == "ms"
    assert res.panel.spec["y"]["unit_provenance"] == "declared by code node c1"
    assert await svc.y_context(res.panel.id, "claude") is None  # no catalog metric behind it
    data = svc.panel_data(res.panel.id, width_px=20)  # narrower than the data: still own step
    assert data["effective_step_ms"] == STEP
    (s,) = data["series"]
    assert s["lo"][0] == pytest.approx(s["avg"][0] - 1) and s["hi"][0] == pytest.approx(
        s["avg"][0] + 1
    )
    assert s["count"][0] is None and s["min"][0] is None  # not given: unknown, never invented
    meta = data["dataset"]
    assert meta["producer"] == {"kind": "code", "node": "c1", "output": "smooth"}
    assert meta["parents"] == [d] and meta["uncertainty"]["level"] == 0.9
    assert "no_uncertainty" not in data["caveats"]
    (note,) = [c for c in data["located"] if c["code"] == "code_output"]
    assert note["severity"] == "info" and "code node c1" in note["message"] and d in note["message"]
    assert data["bucket_state"] == []  # coverage is not judged from code-given counts
    assert not data["overlays"]["normal"]["available"]
    assert not data["overlays"]["ghost"]["available"]
    card = await svc.panel_card(res.panel.id)
    assert card["metrics"] == [] and not card["profile"]["available"]
    assert card["produced_by"]["parents"] == [d]
    assert src.calls == calls  # nothing above went back to the source


async def test_null_counts_are_unknown_not_zero(svc, run):
    d = await _input(svc)
    out = run("c1", [d], lambda: tn.put(_wave(), step_ms=STEP, name="bare"))
    meta = svc.datasets.meta(out["bare"])
    _, result = svc.datasets.get(meta.id)
    summary = svc._time_summary(meta, result, NOW)
    (s,) = summary["series"]
    assert s["gaps"] == 0  # every bucket has a value: no gaps although count is null
    assert s["mean"] == pytest.approx(sum(_wave()["avg"]) / 121)  # plain mean, not None
    assert s["coverage"] is None and s["min"] is None
    assert "no_uncertainty" in summary["caveats"] and "counts_unknown" in summary["caveats"]
    assert "gaps" not in summary["caveats"] and "missing_data" not in summary["caveats"]
    assert summary["produced_by"] == {"code_node": "c1", "output": "bare", "parents": [d]}
    res = svc.show(meta.id, "bare?")
    data = svc.panel_data(res.panel.id, width_px=40)  # coarsened: plain means, count unknown
    assert data["effective_step_ms"] > STEP
    (row,) = data["series"]
    assert all(v is not None for v in row["avg"][1:]) and set(row["count"]) == {None}
    assert "no_uncertainty" in data["caveats"]


async def test_fit_is_refused_by_show_with_what_to_do_instead(svc, run):
    d = await _input(svc)

    def body():
        tn.put_fit(
            "linear", {"slope": {"value": 2.0, "interval": [1.5, 2.5]}, "intercept": 0.3},
            method="OLS", diagnostics={"dw": 2.0}, level=0.95, prediction=_wave(with_interval=True),
            prediction_meta={"step_ms": STEP, "uncertainty": {"method": "OLS", "level": 0.95,
                                                                "kind": "prediction"}},
        )  # fmt: skip

    out = run("c2", [d], body)
    fit, pred = out["fit1"], out["fit1_prediction"]
    with pytest.raises(ValueError, match=rf"fit .*show its prediction {pred}.*'slope'"):
        svc.show(fit, "slope?")
    with pytest.raises(ValueError, match="this is a fit"):
        svc.spectrum(fit)
    svc.show(pred, "prediction")  # the prediction draws


# --- tier-1 ops ----------------------------------------------------------------------------


async def test_ops_that_refetch_are_refused_and_never_reach_the_source(svc, src, run):
    d = await _input(svc)
    out = run("c1", [d], lambda: tn.put(_wave(), step_ms=STEP, name="w"))
    ds = out["w"]
    pid = svc.show(ds, "w").panel.id
    calls = src.calls
    with pytest.raises(ValueError, match="fixed data.*compare_seasonal"):
        await svc.compare_seasonal(ds)
    with pytest.raises(ValueError, match="analyze\\(baseline='previous'\\)"):
        await svc.analyze_reference(ds, "previous")
    for mode in ("previous", "week", "profile"):
        with pytest.raises(ValueError, match="fixed data"):
            await svc.set_marginal(pid, mode, "claude")
    with pytest.raises(ValueError, match="fixed data"):
        await svc.set_overlays(pid, "claude", ghost=True)
    with pytest.raises(ValueError, match="fixed data"):
        await svc.split_outcome(ds, "claude")
    with pytest.raises(ValueError, match="fixed data"):
        await svc.distribution_panel(pid, NOW - 600_000, NOW)
    with pytest.raises(SourceError, match="names a code output"):
        await svc.query("code:c1/w")
    with pytest.raises(SourceError, match="no operating profile"):
        await svc.operating_profile("code:c1/w")
    assert svc.seasonal_suggestion(ds) is None
    assert src.calls == calls


async def test_ops_over_stored_values_work(svc, src, run):
    d = await _input(svc)
    out = run("c1", [d], lambda: tn.put(_wave(), step_ms=STEP, unit="s", name="w"))
    ds = out["w"]
    calls = src.calls
    spec = svc.spectrum(ds)
    (s,) = spec["series"]
    assert s["peaks"] and s["peaks"][0]["period_s"] == pytest.approx(1200, rel=0.1)
    assert svc.analyze(ds)["series"][0]["verdict"]
    for mark in ("spc", "spectrum"):
        svc.panel_data(svc.show(ds, mark, mark=mark).panel.id, width_px=300)
    f = svc.filter(ds, "lowpass", "10m", "smooth")
    fm = svc.datasets.meta(f["dataset"])
    assert fm.code_node == "c1" and fm.unit == "s" and fm.parents == [ds]
    assert "no_uncertainty" in fm.source_caveats  # a filter keeps no declared uncertainty
    p = svc.show(f["dataset"], "filtered").panel
    assert p.spec["y"]["unit"] == "s"
    svc.panel_data(p.id, width_px=200)
    assert src.calls == calls


async def test_fleet_over_a_code_output(svc, src, run):
    d = await _input(svc)

    def body():
        w = _wave()
        frames = [
            w.with_columns(pod=pl.lit(f"p{k}"), avg=pl.col("avg") + k * 0.01 + (5 if k == 7 else 0))
            for k in range(12)
        ]  # fmt: skip
        tn.put(pl.concat(frames), step_ms=STEP, labels=["pod"], name="pods")

    ds = run("c3", [d], body)["pods"]
    calls = src.calls
    out = svc.fleet(ds)
    assert [o["member"] for o in out["outliers"]] == ["pod=p7"]
    svc.show(ds, "pods", mark="fleet")
    assert src.calls == calls


async def test_code_distribution_shows_and_fraction_over_works(svc, run):
    dist = (await svc.query_distribution("lat_bucket", start="now-1h", step="5m"))["dataset"]

    def body():
        tn.put(tn.dataset(dist), like=dist, columns=tn.dataset(dist, "columns"), exact=True)

    ds = run("c4", [dist], body)["out1"]
    pid = svc.show(ds, "latency from code").panel.id
    data = svc.panel_data(pid, width_px=300)
    assert data["dataset"]["producer"]["node"] == "c4"
    fo = svc.fraction_over(ds, 1.0)
    assert fo["series"][0]["n"] > 0
    with pytest.raises(ValueError, match="fixed data"):
        await svc.compare_seasonal(ds)


# --- evidence ------------------------------------------------------------------------------


async def test_evidence_rules_for_code_outputs(svc, run):
    d = await _input(svc)

    def body():
        tn.put(_wave(), step_ms=STEP, name="bare")
        tn.put(_wave(with_interval=True), step_ms=STEP, name="ci",
               uncertainty={"method": "bootstrap", "level": 0.95})  # fmt: skip
        tn.put_fit(
            "linear", {"slope": {"value": 2.0, "interval": [1.5, 2.5]}, "intercept": 0.3},
            method="OLS", diagnostics={"dw": 2.0}, level=0.95,
        )  # fmt: skip

    out = run("c5", [d], body)

    def finding(*refs):
        return svc.ws.finding_create(
            FindingIn(claim="c", scope=_scope(), evidence=list(refs)), "claude"
        )

    def stat(ds, name, value, interval=(1.0, 3.0)):
        return StatisticRef(kind="statistic", dataset=ds, name=name, value=value,
                            interval=interval, method="m")  # fmt: skip

    with pytest.raises(ValueError, match="no_uncertainty"):
        finding(stat(out["bare"], "mean", 10.0))
    bare_panel = svc.show(out["bare"], "bare").panel.id
    with pytest.raises(ValueError, match=f"panel {bare_panel} is not evidence.*no_uncertainty"):
        finding(PanelRef(kind="panel", panel=bare_panel))
    finding(stat(out["ci"], "mean", 10.0))  # declared interval: evidence
    finding(PanelRef(kind="panel", panel=svc.show(out["ci"], "ci").panel.id))
    fit = out["fit1"]
    finding(stat(fit, "slope", 2.0, (1.5, 2.5)))  # as stored
    with pytest.raises(ValueError, match="cite it as stored"):
        finding(stat(fit, "slope", 2.2, (1.5, 2.5)))
    with pytest.raises(ValueError, match="no_uncertainty.*intercept"):
        finding(stat(fit, "intercept", 0.3, (0.2, 0.4)))
    with pytest.raises(ValueError, match="no fit parameter 'r2'"):
        finding(stat(fit, "r2", 0.9, (0.8, 1.0)))


async def test_panel_evidence_checks_every_dataset_of_the_panel(svc, run):
    d = await _input(svc)

    def body():
        tn.put(_wave(), step_ms=STEP, name="bare")
        tn.put(_wave(with_interval=True), step_ms=STEP, name="ci",
               uncertainty={"method": "bootstrap", "level": 0.95})  # fmt: skip

    out = run("c6", [d], body)
    spec = svc.ws.workspace.get_panel(svc.show(out["ci"], "ci").panel.id).spec
    # a panel whose first dataset is evidence but a later one is not: not evidence
    mixed = svc.ws.workspace.create_panel("mixed", spec, [out["ci"], out["bare"]])
    with pytest.raises(ValueError, match=f"panel {mixed.id} is not evidence.*{out['bare']}"):
        svc.ws.finding_create(
            FindingIn(claim="c", scope=_scope(), evidence=[PanelRef(kind="panel", panel=mixed.id)]),
            "claude",
        )
