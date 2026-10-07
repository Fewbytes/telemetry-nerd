"""ElasticsearchSource.discover: _field_caps mapped onto Discovery."""

import json

import pytest

from telemetry_nerd.model.discovery import MetricInfo
from telemetry_nerd.sources.base import Limits

from .es_fake import CAPS, RNG, FakeEs, fixture, search_response


def fake_with(caps_file: dict) -> FakeEs:
    fake = FakeEs(caps=caps_file["fields"], indices=tuple(caps_file["indices"]))
    return fake


async def test_numeric_fields_are_metrics_and_aggregatable_keywords_are_labels():
    fake = fake_with(fixture("field_caps_all.json"))
    d = await fake.source().discover()
    assert d.metrics == (
        MetricInfo("event.duration", None, None, "ns"),
        MetricInfo("host.cpu.pct", "gauge", None, None),
        MetricInfo("http.response.bytes", "counter", None, "B"),
        MetricInfo("http.response.status_code", None, None, None),
    )
    assert d.label_names == ("host.ip", "is_bot", "message.keyword", "service.name", "tier")
    assert d.histograms == {} and d.cardinality is None
    assert d.naming == "fields"
    assert d.metadata_coverage == pytest.approx(3 / 4)
    assert d.caveats == ("cardinality_unavailable", "mapping_conflict:1",
                         "nested_fields_skipped:1", "histogram_fields_skipped:1")  # fmt: skip
    assert d.partial is True
    [req] = [r for r in fake.requests if r.url.path.endswith("/_field_caps")]
    assert req.url.params["fields"] == "*" and req.url.params["include_unmapped"] == "false"


async def test_a_mapping_without_numeric_fields_is_not_an_error():
    caps = {"service.name": CAPS["service.name"], "@timestamp": CAPS["@timestamp"]}
    d = await FakeEs(caps=caps).source().discover()
    assert d.metrics == () and d.label_names == ("service.name",)
    assert d.caveats == ("cardinality_unavailable", "no_numeric_fields")
    assert d.partial is False  # cardinality_unavailable alone is not partial


async def test_too_many_numeric_fields_are_truncated():
    src = fake_with(fixture("field_caps_all.json")).source(limits=Limits(max_metrics=2))
    d = await src.discover()
    assert [m.name for m in d.metrics] == ["event.duration", "host.cpu.pct"]
    assert "metrics_truncated:2/4" in d.caveats and d.partial is True


async def test_discover_clears_the_field_check_cache():
    fake = FakeEs(search=search_response([]))
    src = fake.source()
    stats = json.dumps({"aggs": {"lat": {"stats": {"field": "event.duration"}}}})
    await src.fetch(stats, RNG, 60_000)
    await src.discover()
    await src.fetch(stats, RNG, 60_000)
    assert fake.caps_requests() == ["event.duration", "*", "event.duration"]
