import pytest

from telemetry_nerd.catalog.mergeability import HARTMANN_CAVEAT, NONMERGEABLE_CAVEAT
from telemetry_nerd.catalog.models import Claim
from telemetry_nerd.sources.base import SourceError, SourceUnavailable
from tests.unit.fakes import NOW, FakeSource, make_service

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


async def test_failed_counts_chunk_reaches_quantile_dataset(tmp_path):
    class FlakyCounts(FakeSource):
        async def fetch_values(self, expr, rng, step_ms):
            if "histogram_count" in expr and rng.start_ms <= NOW - 24 * 3_600_000:
                raise SourceUnavailable("counts down")
            return await super().fetch_values(expr, rng, step_ms)

    src = FlakyCounts(values={"histogram_count": 250.0, "histogram_quantile": 0.42})
    svc = make_service(tmp_path, src)
    out = await svc.query(Q, "now-30h", "now-1h", step="1m")
    meta, _ = svc.datasets.get(out["dataset"])
    assert meta.failed_spans
    assert all(r.endswith("counts down") for *_, r in meta.failed_spans)


# -- catalog-flagged non-mergeable statistics (telemetry-nerd-2as.20, spec §5 [H]/[SfE]) ------


def _statistic_claim(value, ts=1):
    return Claim(field="statistic", value=value, origin="rule", confidence=0.6, ts_ms=ts)


async def test_aggregating_a_catalog_flagged_percentile_gauge_is_refused(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    svc.ws.catalog.put_claim("default", "app_latency_p99", _statistic_claim("percentile"))
    with pytest.raises(SourceError) as e:
        await svc.query("avg(app_latency_p99)", "now-2h", "now-1h", step="1m")
    assert "app_latency_p99" in str(e.value)
    assert "allow_nonmergeable" in (e.value.hint or "")


async def test_override_charts_it_anyway_with_hartmann_caveat(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    svc.ws.catalog.put_claim("default", "app_latency_p99", _statistic_claim("percentile"))
    out = await svc.query(
        "avg(app_latency_p99)", "now-2h", "now-1h", step="1m", allow_nonmergeable=True
    )
    assert out["summary"]["caveats"].count(NONMERGEABLE_CAVEAT) == 1  # a short code ...
    assert HARTMANN_CAVEAT not in out["summary"]["caveats"]
    assert out["summary"]["nonmergeable"] == {  # ... and the explanation as its payload
        "uses": ["avg(app_latency_p99)"],
        "explanation": HARTMANN_CAVEAT,
    }


async def test_unflagged_metric_aggregates_without_any_caveat(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    out = await svc.query("avg(some_gauge)", "now-2h", "now-1h", step="1m")
    assert NONMERGEABLE_CAVEAT not in out["summary"]["caveats"]
    assert "nonmergeable" not in out["summary"]


async def test_plain_unwrapped_percentile_gauge_is_not_refused(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    svc.ws.catalog.put_claim("default", "app_latency_p99", _statistic_claim("percentile"))
    out = await svc.query("app_latency_p99", "now-2h", "now-1h", step="1m")
    assert NONMERGEABLE_CAVEAT not in out["summary"]["caveats"]


# whole-dataset operations (2as.31): the same refusal, from the catalog, on a plain gauge
@pytest.mark.parametrize("op", ["fleet", "compare_seasonal", "analyze", "spectrum", "filter"])
async def test_time_and_series_operations_refuse_a_catalog_flagged_percentile_gauge(tmp_path, op):
    svc = make_service(tmp_path, FakeSource())
    svc.ws.catalog.put_claim("default", "app_latency_p99", _statistic_claim("percentile"))
    ds = (await svc.query("app_latency_p99", "now-2h", "now-1h", step="1m"))["dataset"]
    with pytest.raises(ValueError, match="already-computed percentile") as e:
        svc.signal.check(ds, op)
    assert "app_latency_p99" in str(e.value) and "fraction_over" in str(e.value)


async def test_fleet_and_seasonal_reach_the_same_refusal(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    svc.ws.catalog.put_claim("default", "app_latency_p99", _statistic_claim("percentile"))
    ds = (await svc.query("app_latency_p99", "now-2h", "now-1h", step="1m"))["dataset"]
    with pytest.raises(ValueError, match="already-computed percentile"):
        svc.fleets.check(ds)
    with pytest.raises(ValueError, match="already-computed percentile"):
        svc.seasonal.check(ds)


async def test_an_unflagged_gauge_passes_the_same_checks(tmp_path):
    svc = make_service(tmp_path, FakeSource())
    ds = (await svc.query("some_gauge", "now-2h", "now-1h", step="1m"))["dataset"]
    svc.signal.check(ds, "fleet")  # no raise


async def test_a_pack_claim_makes_a_known_summary_non_aggregatable(tmp_path):
    from telemetry_nerd.model.discovery import Discovery, MetricInfo

    d = Discovery(
        (MetricInfo("go_gc_duration_seconds", "summary"), MetricInfo("go_threads", "gauge")),
        (), {}, None, 1.0, (), False,
    )  # fmt: skip
    svc = make_service(tmp_path, FakeSource(name="default", discovery=d))
    await svc.learn("default")
    e = svc.ws.catalog_entry("default", "go_gc_duration_seconds")
    assert e.fields["statistic"].value == "percentile" and e.fields["statistic"].origin == "pack"
    with pytest.raises(SourceError, match="go_gc_duration_seconds"):
        await svc.query("avg(go_gc_duration_seconds)", "now-2h", "now-1h", step="1m")
    await svc.query("avg(go_threads)", "now-2h", "now-1h", step="1m")  # not flagged
