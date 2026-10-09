import pytest

from telemetry_nerd.catalog.rules import normalize_unit
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import FakeSource, make_service


def disc(*infos, caveats=(), histograms=None):
    return Discovery(
        metrics=tuple(infos),
        label_names=("job",),
        histograms=histograms or {},
        cardinality=None,
        metadata_coverage=1.0,
        caveats=tuple(caveats),
        partial=bool(caveats),
    )


DISCOVERY = disc(
    MetricInfo("reqs_total", "counter", "requests served", None),
    MetricInfo("lat_seconds_bucket"),
    MetricInfo("lat_seconds_sum"),
    MetricInfo("lat_seconds_count"),
    MetricInfo("queue_depth", "gauge", "items waiting", "1"),
    MetricInfo("up"),
    histograms={"lat_seconds": "classic"},
)


@pytest.fixture
def svc(tmp_path):
    return make_service(tmp_path, FakeSource(name="default", discovery=DISCOVERY))


async def test_learn_writes_t0_claims_with_provenance(svc):
    out = await svc.learn("default")
    # 6 listed series + the classic histogram's base name lat_seconds (6gp)
    assert out["metrics"] == 7 and out["new"] == 7 and out["claims_changed"] > 0
    e = svc.ws.catalog_entry("default", "reqs_total")
    assert e.fields["type"].value == "counter"
    assert e.fields["type"].origin == "metadata"  # declared beats the _total rule
    assert {c.origin for c in e.claims["type"]} == {"metadata", "rule"}
    assert e.fields["description"].value == "requests served"
    fam = svc.ws.catalog_entry("default", "lat_seconds_bucket").fields["histogram_family"]
    assert fam.value == ["lat_seconds_bucket", "lat_seconds_sum", "lat_seconds_count"]
    # a "1" unit is dimensionless: no unit claim, and `up` stays unlearned beyond the inventory
    assert "unit" not in svc.ws.catalog_entry("default", "queue_depth").fields
    assert svc.ws.catalog_entry("default", "up").fields == {}


async def test_learn_emits_one_event_not_one_per_claim(svc):
    await svc.learn("default")
    types = [e.type for e in svc.ws.log.since(0)]
    assert types.count("catalog.learned") == 1 and "catalog.claimed" not in types


async def test_relearn_of_an_unchanged_source_writes_nothing(svc):
    await svc.learn("default")
    ts = {c.ts_ms for e in svc.ws.catalog_list("default") for cs in e.claims.values() for c in cs}
    svc.ws.clock = lambda: 99_999_999_999
    again = await svc.learn("default")
    assert again["claims_changed"] == 0 and again["new"] == 0 and again["removed"] == 0
    after = {
        c.ts_ms for e in svc.ws.catalog_list("default") for cs in e.claims.values() for c in cs
    }
    assert after == ts


async def test_relearn_diffs_and_keeps_claims_of_removed_metrics(svc):
    await svc.learn("default")
    svc.sources.get("default").discovery = disc(
        MetricInfo("reqs_total", "counter"), MetricInfo("new_total")
    )
    out = await svc.learn("default")
    assert out["new"] == 1 and out["removed"] == 6
    gone = svc.ws.catalog_entry("default", "lat_seconds_bucket")
    assert not gone.present and "histogram_family" in gone.fields


async def test_truncated_discovery_never_removes(svc):
    await svc.learn("default")
    svc.sources.get("default").discovery = disc(
        MetricInfo("reqs_total"), caveats=("metrics_truncated:1/6",)
    )
    out = await svc.learn("default")
    assert out["removed"] == 0 and out["complete"] is False
    assert svc.ws.catalog_entry("default", "up").present


async def test_user_and_claude_claims_survive_relearning(svc):
    await svc.learn("default")
    svc.ws.catalog_claim("default", "reqs_total", "unit", "requests", "user", "user")
    await svc.learn("default")
    e = svc.ws.catalog_entry("default", "reqs_total")
    assert e.fields["unit"].value == "requests" and e.fields["unit"].origin == "user"


async def test_chart_unit_comes_from_the_catalog_with_provenance(svc):
    ds = (await svc.query("rate(reqs_total[5m])", start="now-2h", end="now-1h"))["dataset"]
    before = svc.show(ds, "rate?").panel.spec["y"]
    assert (before["unit"], before["unit_provenance"]) == ("count/s", "inferred from metric name")

    await svc.learn("default")
    svc.ws.catalog_claim("default", "reqs_total", "unit", "requests", "user", "user")
    ds2 = (await svc.query("rate(reqs_total[5m])", start="now-2h", end="now-1h"))["dataset"]
    after = svc.show(ds2, "rate?").panel.spec["y"]
    assert (after["unit"], after["unit_provenance"]) == ("requests/s", "set by user")


async def test_declared_unit_beats_name_rule_on_charts(tmp_path):
    d = disc(MetricInfo("work_total", "counter", None, "seconds"))
    svc = make_service(tmp_path, FakeSource(name="default", discovery=d))
    await svc.learn("default")
    ds = (await svc.query("work_total", start="now-2h", end="now-1h"))["dataset"]
    y = svc.show(ds, "work?").panel.spec["y"]
    assert (y["unit"], y["unit_provenance"]) == ("s", "source metadata")


async def test_classic_histogram_base_name_is_catalogued(svc):
    """telemetry-nerd-6gp: listing __name__ yields only X_bucket/_sum/_count; the histogram X
    gets its own entry so bindings and suggestions can name it."""
    await svc.learn("default")
    assert svc.ws.catalog.has_metric("default", "lat_seconds")
    e = svc.ws.catalog_entry("default", "lat_seconds")
    assert e.fields["type"].value == "histogram"
    assert e.fields["histogram_family"].value == [
        "lat_seconds_bucket", "lat_seconds_sum", "lat_seconds_count",
    ]  # fmt: skip
    assert e.fields["unit"].value == "s" and not e.is_family
    assert svc.ws.catalog_facts("default", "lat_seconds").type == "histogram"


async def test_histogram_base_listed_by_the_source_is_not_duplicated(tmp_path):
    d = disc(
        MetricInfo("lat_seconds", "histogram", "latency", "seconds"),
        MetricInfo("lat_seconds_bucket"),
        MetricInfo("lat_seconds_sum"),
        MetricInfo("lat_seconds_count"),
        histograms={"lat_seconds": "classic"},
    )
    svc = make_service(tmp_path, FakeSource(name="default", discovery=d))
    out = await svc.learn("default")
    assert out["metrics"] == 4
    e = svc.ws.catalog_entry("default", "lat_seconds")
    assert e.fields["type"].origin == "metadata" and e.fields["description"].value == "latency"


ES_DISCOVERY = Discovery(
    metrics=(
        MetricInfo("event.duration", None, None, "ns"),
        MetricInfo("requests_total"),  # a field name, not a counter by naming rule
        MetricInfo("http.response.bytes", "counter", None, "B"),
        MetricInfo("http_request_duration_seconds"),  # no _seconds rule, no pack claim
    ),
    label_names=("service.name",),
    histograms={},
    cardinality=None,
    metadata_coverage=0.5,
    caveats=("cardinality_unavailable",),
    partial=False,
    naming="fields",
)


async def test_field_paths_get_declared_metadata_and_exact_pack_claims_only(tmp_path):
    """Field paths (Elasticsearch/OpenSearch) never get T0 name-suffix rules (requests_total,
    http_request_duration_seconds misread as Prometheus conventions), but an exact-name knowledge
    pack entry (ecs.toml's event.duration) still applies and, ranking above metadata, wins."""
    svc = make_service(tmp_path, FakeSource(name="default", discovery=ES_DISCOVERY))
    out = await svc.learn("default")
    assert (out["metrics"], out["families"], out["family_members"]) == (4, 0, 0)
    assert out["relations_changed"] == 0
    dur = svc.ws.catalog_entry("default", "event.duration").fields["unit"]
    assert (dur.value, dur.origin) == ("ns", "pack")
    assert svc.ws.catalog_entry("default", "requests_total").fields == {}
    assert svc.ws.catalog_entry("default", "http_request_duration_seconds").fields == {}
    b = svc.ws.catalog_entry("default", "http.response.bytes")
    assert (b.fields["type"].value, b.fields["unit"].value) == ("counter", "B")
    origins = {c.origin for e in svc.ws.catalog_list("default")
               for cs in e.claims.values() for c in cs}  # fmt: skip
    assert origins == {"metadata", "pack"}


def test_declared_byte_unit_symbol_normalizes():
    assert normalize_unit("B") == "B"
