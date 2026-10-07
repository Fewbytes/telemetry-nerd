"""Elasticsearch 8.x and OpenSearch 2.x end to end through the service (the epic's primary
validation tier; spec: Testing)."""

import json

import pytest

from telemetry_nerd.config import Settings
from telemetry_nerd.core.bootstrap import build_service
from telemetry_nerd.sources.base import LimitExceeded, SourceError
from telemetry_nerd.sources.spec import SourceSpec
from tests.integration.es_seed import GAP_MINUTE, MINUTES, MS, PATTERN, base_ms, seed

pytestmark = pytest.mark.integration

CHECKOUT = {"query_string": {"query": "service.name:checkout"}}


def expr(**doc) -> str:
    return json.dumps(doc)


@pytest.fixture(params=["elasticsearch", "opensearch"])
def cluster(request):
    return request.param, request.getfixturevalue(
        "es_url" if request.param == "elasticsearch" else "os_url"
    )


@pytest.fixture
async def seeded(cluster, tmp_path):
    flavor, url = cluster
    base = base_ms()
    seed(url, base)
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    out = await svc.source_connect(SourceSpec(name="es", url=url, flavor=flavor,
                                              index_pattern=PATTERN, time_field="@timestamp"))  # fmt: skip
    return {"svc": svc, "connect": out, "base": base, "flavor": flavor, "url": url}


def window(base: int) -> dict:
    # query buckets ending base + 1 m .. base + 30 m cover the seeded minutes 0 .. 29
    return {"start": str(base + 60_000), "end": str(base + MINUTES * 60_000), "step": "1m",
            "source": "es"}  # fmt: skip


def ends(base: int) -> list[int]:
    return [base + (m + 1) * 60_000 for m in range(MINUTES)]


async def test_probe_reads_each_flavor(seeded):
    status = seeded["connect"]["status"]
    assert status["reachable"] is True and status["distribution"] == seeded["flavor"]
    assert status["version"] and "flavor_mismatch" not in status and status["indices"] >= 1


async def test_opensearch_connected_as_elasticsearch_reports_the_mismatch(cluster, tmp_path):
    flavor, url = cluster
    if flavor != "opensearch":
        pytest.skip("the mismatch is OpenSearch reached as elasticsearch")
    seed(url, base_ms())
    svc = build_service(Settings(data_dir=tmp_path / "d", source_url="http://127.0.0.1:9"))
    out = await svc.source_connect(SourceSpec(name="os", url=url, flavor="elasticsearch",
                                              index_pattern=PATTERN, time_field="@timestamp"))  # fmt: skip
    assert "opensearch" in out["status"]["flavor_mismatch"]


async def test_rate_is_documents_per_second_with_an_interior_zero(seeded):
    svc, base = seeded["svc"], seeded["base"]
    out = await svc.query(expr(query=CHECKOUT), **window(base))
    _, res = svc.datasets.get(out["dataset"])
    got = {r["ts_ms"]: r["avg"] for r in res.buckets.to_pylist()}
    assert sorted(got) == ends(base)
    for m, ts in enumerate(ends(base)):
        assert got[ts] == pytest.approx(0.0 if m == GAP_MINUTE else 12 / 60), m


async def test_field_rate_counts_the_field_per_second(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT, aggs={"n": {"value_count": {"field": "event.duration"}}})
    _, res = svc.datasets.get((await svc.query(q, **window(base)))["dataset"])
    vals = {r["ts_ms"]: r["avg"] for r in res.buckets.to_pylist()}
    assert vals[ends(base)[0]] == pytest.approx(0.2) and vals[ends(base)[GAP_MINUTE]] == 0.0


async def test_stats_is_the_mean_latency_with_its_count(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT, aggs={"lat": {"stats": {"field": "event.duration"}}})
    _, res = svc.datasets.get((await svc.query(q, **window(base)))["dataset"])
    rows = {r["ts_ms"]: r for r in res.buckets.to_pylist()}
    assert ends(base)[GAP_MINUTE] not in rows and len(rows) == MINUTES - 1
    r = rows[ends(base)[0]]
    assert (r["count"], r["min"], r["max"]) == (12, 40 * MS, 100 * MS)
    assert r["avg"] == pytest.approx(50 * MS)


async def test_grouped_rate_is_one_series_per_status(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT, aggs={"by_status": {"terms": {"field": "http.response.status_code"}}})
    _, res = svc.datasets.get((await svc.query(q, **window(base)))["dataset"])
    labels = {r["series_id"]: json.loads(r["labels"])["http.response.status_code"]
              for r in res.series.to_pylist()}  # fmt: skip
    per = {(labels[r["series_id"]], r["ts_ms"]): r["avg"] for r in res.buckets.to_pylist()}
    assert set(labels.values()) == {"200", "500"}
    assert per[("200", ends(base)[0])] == pytest.approx(10 / 60)
    assert per[("500", ends(base)[0])] == pytest.approx(2 / 60)
    assert per[("500", ends(base)[GAP_MINUTE])] == 0.0


async def test_percentile_is_a_quantile_with_its_count(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(query=CHECKOUT,
             aggs={"p": {"percentiles": {"field": "event.duration", "percents": [50]}}})  # fmt: skip
    out = await svc.query(q, **window(base))
    meta, res = svc.datasets.get(out["dataset"])
    assert meta.representation == "quantile" and "approximate_percentile" in meta.source_caveats
    r = next(r for r in res.buckets.to_pylist() if r["ts_ms"] == ends(base)[0])
    assert r["count"] == 12 and r["avg"] == pytest.approx(40 * MS, rel=0.01)


async def test_query_distribution_is_query_chosen_document_counts(seeded):
    svc, base = seeded["svc"], seeded["base"]
    sel = expr(aggs={"lat": {"histogram": {"field": "event.duration", "interval": 25 * MS}}})
    out = await svc.query_distribution(sel, ["service.name"], **window(base))
    assert out["summary"]["buckets"].startswith("fixed-width buckets of")
    assert out["summary"]["buckets"].endswith("chosen by the query; each [lo, hi)")
    _meta, dist = svc.datasets.get_distribution(out["dataset"])
    labels = {r["series_id"]: json.loads(r["labels"])["service.name"]
              for r in dist.series.to_pylist()}  # fmt: skip
    first = [(labels[r["series_id"]], r["bucket_lo"], r["bucket_hi"], r["count"])
             for r in dist.rows.to_pylist() if r["ts_ms"] == ends(base)[0]]  # fmt: skip
    assert sorted(first) == [
        ("checkout", 25 * MS, 50 * MS, 10.0), ("checkout", 100 * MS, 125 * MS, 2.0),
        ("search", 0.0, 25 * MS, 5.0),
    ]  # fmt: skip
    cols = {(labels[c["series_id"]], c["ts_ms"]): c["n"] for c in dist.columns.to_pylist()}
    assert cols[("checkout", ends(base)[GAP_MINUTE])] == 0.0
    assert cols[("search", ends(base)[0])] == 5.0
    f = svc.fraction_over(out["dataset"], 50 * MS)
    assert f["compare"] == ">=" and f["series"][0]["exact"] is True
    assert f["series"][0]["fraction"] == pytest.approx(2 / 17, rel=1e-3)  # 4 significant digits


async def test_discover_learns_fields_and_their_declared_units(seeded):
    svc = seeded["svc"]
    out = await svc.learn("es")
    assert out["metrics"] >= 2 and out["families"] == 0
    dur = svc.ws.catalog_entry("es", "event.duration").fields["unit"]
    assert (dur.value, dur.origin) == ("ns", "metadata")
    assert "http.response.status_code" in {e.metric for e in svc.ws.catalog_list("es")}


async def test_an_unmapped_field_is_refused_not_read_as_no_data(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(aggs={"lat": {"stats": {"field": "event.durationn"}}})
    with pytest.raises(SourceError, match="not in the mapping"):
        await svc.query(q, **window(base))


async def test_terms_on_a_text_field_points_at_the_keyword_sub_field(seeded):
    svc, base = seeded["svc"], seeded["base"]
    with pytest.raises(SourceError, match="not aggregatable") as e:
        await svc.query(expr(aggs={"m": {"terms": {"field": "message"}}}), **window(base))
    assert "message.keyword" in e.value.hint


async def test_a_too_small_terms_size_is_refused(seeded):
    svc, base = seeded["svc"], seeded["base"]
    q = expr(aggs={"s": {"terms": {"field": "service.name", "size": 1}}})
    with pytest.raises(LimitExceeded, match="other terms"):
        await svc.query(q, **window(base))
