"""EsQuery: the Elasticsearch expr contract (parse, validate, build the request; no I/O)."""

import json

import pytest

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import SourceError
from telemetry_nerd.sources.esquery import COUNT_AGG, GROUP_AGG, TIME_AGG, EsQuery

QS = {"query_string": {"query": "service.name:checkout"}}
START = 1_700_000_040_000  # a multiple of 60 s
RNG = TimeRange(START, START + 240_000)
STATS = {"lat": {"stats": {"field": "event.duration"}}}


def expr(**doc) -> str:
    return json.dumps(doc)


def test_query_without_aggs_is_the_rate_form():
    q = EsQuery.parse(expr(query=QS))
    assert (q.form, q.query, q.field, q.group) == ("rate", QS, None, None)


def test_absent_query_means_all_documents():
    assert EsQuery.parse("{}").query == {"match_all": {}}


@pytest.mark.parametrize(
    ("agg", "form"),
    [
        ({"n": {"value_count": {"field": "event.duration"}}}, "field_rate"),
        (STATS, "stats"),
        ({"p": {"percentiles": {"field": "event.duration", "percents": [99]}}}, "percentile"),
        ({"h": {"histogram": {"field": "event.duration", "interval": 25}}}, "histogram"),
    ],
)
def test_metric_forms(agg, form):
    q = EsQuery.parse(expr(query=QS, aggs=agg))
    assert (q.form, q.field, q.metric_name, q.group) == (
        form,
        "event.duration",
        next(iter(agg)),
        None,
    )
    assert q.metric == next(iter(agg.values()))


def test_aggregations_is_the_es_synonym_of_aggs():
    assert EsQuery.parse(expr(aggregations=STATS)).form == "stats"


@pytest.mark.parametrize(("percents", "q"), [([99], 0.99), ([50], 0.5), ([99.9], 0.999)])
def test_percentile_q_is_a_fraction(percents, q):
    p = EsQuery.parse(expr(aggs={"p": {"percentiles": {"field": "x", "percents": percents}}}))
    assert p.q == pytest.approx(q)


def test_histogram_interval_and_offset():
    h = EsQuery.parse(expr(aggs={"h": {"histogram": {"field": "x", "interval": 25, "offset": 5}}}))
    assert (h.interval, h.offset) == (25.0, 5.0)
    plain = EsQuery.parse(expr(aggs={"h": {"histogram": {"field": "x", "interval": 25}}}))
    assert plain.offset == 0.0


def test_terms_without_sub_aggregation_is_the_grouped_rate_form():
    q = EsQuery.parse(expr(aggs={"by_status": {"terms": {"field": "http.response.status_code"}}}))
    assert (q.form, q.group_name, q.group_field, q.metric) == (
        "rate",
        "by_status",
        "http.response.status_code",
        None,
    )


def test_terms_wrapping_a_metric_groups_it():
    q = EsQuery.parse(
        expr(
            aggs={
                "by_status": {
                    "terms": {"field": "http.response.status_code", "size": 50},
                    "aggs": STATS,
                }
            }
        )
    )
    assert (q.form, q.metric_name, q.field) == ("stats", "lat", "event.duration")
    assert (q.group_name, q.group, q.group_field) == (
        "by_status",
        {"field": "http.response.status_code", "size": 50},
        "http.response.status_code",
    )


def _terms(sub):
    return {"t": {"terms": {"field": "s"}, "aggs": sub}}


REFUSALS = [
    ("not json", "not JSON"),
    ("[1, 2]", "JSON object"),
    (expr(query=QS, size=0), "unexpected keys"),
    (expr(aggs=STATS, aggregations=STATS), "not both"),
    (expr(query="service:x"), "query must be a JSON object"),
    (expr(aggs={}), "exactly one"),
    (expr(aggs={"a": {"stats": {"field": "x"}}, "b": {"stats": {"field": "y"}}}), "exactly one"),
    (expr(aggs={"a": {"avg": {"field": "x"}}}), "avg is not supported"),
    (
        expr(aggs={"a": {"date_histogram": {"field": "@timestamp"}}}),
        "date_histogram is not supported",
    ),
    (expr(aggs={"a": {"stats": {"script": {"source": "doc['x'].value"}}}}), "scripts"),
    (expr(aggs={"a": {"stats": {}}}), "needs a field"),
    (expr(aggs={"a": {"terms": {"size": 5}}}), "needs a field"),
    (
        expr(aggs={"p": {"percentiles": {"field": "x", "percents": [50, 99]}}}),
        "exactly one percents",
    ),
    (expr(aggs={"p": {"percentiles": {"field": "x"}}}), "exactly one percents"),
    (expr(aggs={"p": {"percentiles": {"field": "x", "percents": [100]}}}), "exactly one percents"),
    (expr(aggs={"__tn_x": {"stats": {"field": "x"}}}), "reserved"),
    (expr(aggs=_terms({"__tn_n": {"stats": {"field": "x"}}})), "reserved"),
    (expr(aggs=_terms({"u": {"terms": {"field": "v"}}})), "one terms level"),
    (
        expr(aggs=_terms({"h": {"histogram": {"field": "x", "interval": 5}}})),
        "query_distribution's by",
    ),
    (expr(aggs=_terms({"l": {"stats": {"field": "x"}, "aggs": STATS}})), "nest at most"),
    (expr(aggs={"l": {"stats": {"field": "x"}, "aggs": STATS}}), "takes no sub-aggregations"),
    (expr(aggs={"h": {"histogram": {"field": "x"}}}), "positive numeric interval"),
    (expr(aggs={"h": {"histogram": {"field": "x", "interval": 5, "keyed": True}}}), "keyed"),
]


@pytest.mark.parametrize(("text", "match"), REFUSALS)
def test_unsupported_shapes_are_refused_with_the_accepted_forms(text, match):
    with pytest.raises(SourceError, match=match) as e:
        EsQuery.parse(text)
    assert "value_count" in e.value.hint and "percentiles" in e.value.hint


def test_body_grafts_the_callers_aggregation_under_the_time_buckets():
    q = EsQuery.parse(expr(query=QS, aggs=STATS))
    assert q.body(RNG, 60_000, "@timestamp", 30.0) == {
        "size": 0,
        "track_total_hits": False,
        "timeout": "30000ms",
        "query": {
            "bool": {
                "filter": [
                    QS,
                    {
                        "range": {
                            "@timestamp": {
                                "gte": START - 60_000,
                                "lt": START + 240_000,
                                "format": "epoch_millis",
                            }
                        }
                    },
                ]
            }
        },
        "aggs": {
            TIME_AGG: {
                "date_histogram": {
                    "field": "@timestamp",
                    "fixed_interval": "60000ms",
                    "offset": "+0ms",
                    "min_doc_count": 0,
                },
                "aggs": STATS,
            }
        },
    }


def test_rate_form_sends_no_sub_aggregation():
    body = EsQuery.parse(expr(query=QS)).body(RNG, 60_000, "@timestamp", 30.0)
    assert "aggs" not in body["aggs"][TIME_AGG]


def test_offset_aligns_the_interval_grid_to_the_range():
    body = EsQuery.parse("{}").body(TimeRange(START + 15_000, START + 75_000), 60_000, "ts", 30.0)
    assert body["aggs"][TIME_AGG]["date_histogram"]["offset"] == "+15000ms"


def test_percentile_adds_a_value_count_sibling_for_the_count():
    p = EsQuery.parse(
        expr(aggs={"p": {"percentiles": {"field": "event.duration", "percents": [99]}}})
    )
    inner = p.body(RNG, 60_000, "@timestamp", 30.0)["aggs"][TIME_AGG]["aggs"]
    assert inner == {
        "p": {"percentiles": {"field": "event.duration", "percents": [99]}},
        COUNT_AGG: {"value_count": {"field": "event.duration"}},
    }


def test_grouped_form_nests_the_metric_under_terms():
    q = EsQuery.parse(
        expr(
            aggs={
                "by_status": {
                    "terms": {"field": "http.response.status_code", "size": 50},
                    "aggs": STATS,
                }
            }
        )
    )
    inner = q.body(RNG, 60_000, "@timestamp", 30.0)["aggs"][TIME_AGG]["aggs"]
    assert inner == {
        "by_status": {"terms": {"field": "http.response.status_code", "size": 50}, "aggs": STATS}
    }


def test_with_group_builds_the_terms_level_from_by():
    h = EsQuery.parse(expr(aggs={"h": {"histogram": {"field": "x", "interval": 5}}}))
    g = h.with_group("service.name", 501)
    assert (g.group_name, g.group, g.group_field) == (
        GROUP_AGG,
        {"field": "service.name", "size": 501},
        "service.name",
    )
    assert g.form == "histogram" and h.group is None  # frozen: a new query


def test_fields_names_every_aggregated_field_with_what_it_needs():
    grouped = EsQuery.parse(expr(aggs={"t": {"terms": {"field": "code"}, "aggs": STATS}}))
    assert grouped.fields() == [("event.duration", "numeric"), ("code", "aggregatable")]
    count = EsQuery.parse(expr(aggs={"n": {"value_count": {"field": "user.id"}}}))
    assert count.fields() == [("user.id", "aggregatable")]
    assert EsQuery.parse(expr(query=QS)).fields() == []
