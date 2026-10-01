import pytest

from telemetry_nerd.sources.base import SourceError
from tests.unit.fakes import FakeSource, make_service

Q = 'histogram_quantile(0.95, sum by (r) (rate(lat{s="c"}[5m])))'


def svc_with(tmp_path, n=250.0, q=0.42):
    src = FakeSource(values={"histogram_count": n, "histogram_quantile": q})
    return make_service(tmp_path, src), src


async def test_quantile_dataset_carries_n_and_is_not_rolled_up(tmp_path):
    svc, src = svc_with(tmp_path)
    out = await svc.query(Q, "now-2h", "now-1h", step="1m")
    meta, result = svc.datasets.get(out["dataset"])
    assert meta.representation == "quantile"
    assert meta.quantile == 0.95 and meta.n_min == 200
    rows = result.buckets.to_pylist()
    assert {r["count"] for r in rows} == {250}
    assert {r["avg"] for r in rows} == {0.42}
    assert any("histogram_count" in e for e in src.value_exprs)
    assert src.calls >= 2  # values + counts, never the rollup fetch


async def test_aggregated_percentile_is_refused_with_hint(tmp_path):
    svc, _ = svc_with(tmp_path)
    with pytest.raises(SourceError) as e:
        await svc.query(f"avg({Q})", "now-2h", "now-1h", step="1m")
    assert "percentile" in str(e.value)
    assert "histogram first" in (e.value.hint or "")


async def test_rate_interval_is_expanded_for_any_expression(tmp_path):
    src = FakeSource(resolution_ms=20_000)
    svc = make_service(tmp_path, src)
    out = await svc.query("sum(rate(x[$__rate_interval]))", "now-2h", "now-1h", step="30s")
    meta, _ = svc.datasets.get(out["dataset"])
    assert meta.expr == "sum(rate(x[80s]))"


async def test_unknown_n_keeps_values_and_flags(tmp_path):
    src = FakeSource(values={"histogram_quantile": 0.3})
    svc = make_service(tmp_path, src)
    mixed = "histogram_quantile(0.9, sum by (le) (rate(a_bucket[1m])) + sum by (le) (rate(b_bucket[5m])))"
    out = await svc.query(mixed, "now-2h", "now-1h", step="1m")
    meta, _ = svc.datasets.get(out["dataset"])
    assert meta.representation == "quantile" and meta.n_min is None
    assert "n_unknown" in out["summary"]["caveats"]


async def test_panel_data_never_rebuckets_quantiles(tmp_path):
    svc, _ = svc_with(tmp_path)
    out = await svc.query(Q, "now-6h", "now-1h", step="15s")
    panel = svc.show(out["dataset"], "p95 by r?").panel
    data = svc.panel_data(panel.id, width_px=100)  # 1201 buckets >> 100 px
    assert data["effective_step_ms"] == 15_000
    assert data["dataset"]["n_min"] == 200
