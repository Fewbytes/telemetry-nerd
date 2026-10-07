"""ElasticsearchSource.fetch / fetch_values: forms, grouping, field checks and the error table."""

import json

import httpx
import pytest

from telemetry_nerd.model.series import series_id
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.elasticsearch import _Forbidden

from .es_fake import CAPS, PATTERN, RNG, START, FakeEs, es_error, fixture, search_response, tb

QS = {"query_string": {"query": "service.name:checkout"}}
RATE = json.dumps({"query": QS})
STATS = json.dumps({"query": QS, "aggs": {"lat": {"stats": {"field": "event.duration"}}}})
COUNT = json.dumps({"aggs": {"n": {"value_count": {"field": "event.duration"}}}})
P99 = json.dumps({"aggs": {"p": {"percentiles": {"field": "event.duration", "percents": [99]}}}})
BY_STATUS_RATE = json.dumps(
    {"aggs": {"by_status": {"terms": {"field": "http.response.status_code"}}}}
)
BY_STATUS_STATS = json.dumps(
    {
        "aggs": {
            "by_status": {
                "terms": {"field": "http.response.status_code"},
                "aggs": {"lat": {"stats": {"field": "event.duration"}}},
            }
        }
    }
)
HIST = json.dumps({"aggs": {"h": {"histogram": {"field": "event.duration", "interval": 25}}}})
M = 60_000


def rows(result) -> list[dict]:
    return result.buckets.to_pylist()


def labels_of(result) -> dict[str, dict]:
    return {r["series_id"]: json.loads(r["labels"]) for r in result.series.to_pylist()}


RATE_BUCKETS = [tb(START - M, 0), tb(START, 120), tb(START + M, 0), tb(START + 2 * M, 60),
                tb(START + 3 * M, 0)]  # fmt: skip


async def test_rate_is_documents_per_second_at_bucket_end_times_with_interior_zeros():
    fake = FakeEs(search=search_response(RATE_BUCKETS))
    res = await fake.source().fetch(RATE, RNG, M)
    sid = series_id("es", {})
    assert rows(res) == [
        {"ts_ms": START + M, "series_id": sid, "avg": 2.0, "min": 2.0, "max": 2.0, "count": 1},
        {"ts_ms": START + 2 * M, "series_id": sid, "avg": 0.0, "min": 0.0, "max": 0.0, "count": 1},
        {"ts_ms": START + 3 * M, "series_id": sid, "avg": 1.0, "min": 1.0, "max": 1.0, "count": 1},
    ]  # leading (ends START) and trailing (ends START + 4 m) empty buckets: no rows
    assert labels_of(res) == {sid: {}}
    [body] = fake.searches()
    assert body["aggs"]["__tn_time"]["date_histogram"]["fixed_interval"] == "60000ms"
    window = body["query"]["bool"]["filter"][1]["range"]["@timestamp"]
    assert (window["gte"], window["lt"]) == (START - M, START + 4 * M)
    assert body["query"]["bool"]["filter"][0] == QS


async def test_rate_needs_no_field_check():
    fake = FakeEs(search=search_response(RATE_BUCKETS))
    await fake.source().fetch(RATE, RNG, M)
    assert fake.caps_requests() == []


async def test_field_rate_is_value_count_per_second():
    buckets = [tb(START, 30, n={"value": 30}), tb(START + M, 0, n={"value": 0}),
               tb(START + 2 * M, 6, n={"value": 3})]  # fmt: skip
    res = await FakeEs(search=search_response(buckets)).source().fetch(COUNT, RNG, M)
    assert [(r["ts_ms"], r["avg"], r["count"]) for r in rows(res)] == [
        (START + M, 0.5, 1),
        (START + 2 * M, 0.0, 1),
        (START + 3 * M, 0.05, 1),
    ]


async def test_stats_keeps_mean_min_max_and_the_documents_that_had_the_field():
    buckets = [
        tb(START, 12, lat={"count": 12, "min": 40.0, "max": 100.0, "avg": 50.0, "sum": 600.0}),
        tb(START + M, 4, lat={"count": 0, "min": None, "max": None, "avg": None, "sum": 0.0}),
        tb(START + 2 * M, 1, lat={"count": 1, "min": 7.0, "max": 7.0, "avg": 7.0, "sum": 7.0}),
    ]
    res = await FakeEs(search=search_response(buckets)).source().fetch(STATS, RNG, M)
    assert [(r["ts_ms"], r["avg"], r["min"], r["max"], r["count"]) for r in rows(res)] == [
        (START + M, 50.0, 40.0, 100.0, 12),
        (START + 3 * M, 7.0, 7.0, 7.0, 1),
    ]  # count 0: no observation, no row


async def test_fetch_refuses_a_percentile_and_fetch_values_reads_it_with_its_count():
    buckets = [
        tb(START, 40, p={"values": {"99.0": 812.5}}, __tn_n={"value": 40}),
        tb(START + M, 5, p={"values": {"99.0": None}}, __tn_n={"value": 0}),
    ]
    fake = FakeEs(search=search_response(buckets))
    src = fake.source()
    with pytest.raises(SourceError, match="never rolled up"):
        await src.fetch(P99, RNG, M)
    res = await src.fetch_values(P99, RNG, M)
    assert [(r["ts_ms"], r["avg"], r["count"]) for r in rows(res)] == [(START + M, 812.5, 40)]
    inner = fake.searches()[-1]["aggs"]["__tn_time"]["aggs"]
    assert inner["__tn_n"] == {"value_count": {"field": "event.duration"}}


async def test_percentile_values_also_parse_the_unkeyed_list_shape():
    buckets = [tb(START, 3, p={"values": [{"key": 99.0, "value": 9.0}]}, __tn_n={"value": 3})]
    res = await FakeEs(search=search_response(buckets)).source().fetch_values(P99, RNG, M)
    assert [r["avg"] for r in rows(res)] == [9.0]


async def test_grouped_stats_are_one_series_per_term_and_absent_terms_have_no_row():
    res = (
        await FakeEs(search=fixture("search_stats_by_status.json"))
        .source()
        .fetch(BY_STATUS_STATS, RNG, M)
    )
    s200 = series_id("es", {"http.response.status_code": "200"})
    s500 = series_id("es", {"http.response.status_code": "500"})
    assert labels_of(res) == {s200: {"http.response.status_code": "200"},
                              s500: {"http.response.status_code": "500"}}  # fmt: skip
    got = {(r["series_id"], r["ts_ms"]): (r["avg"], r["count"]) for r in rows(res)}
    assert got == {
        (s200, START + M): (40.0, 10), (s200, START + 2 * M): (41.0, 10),
        (s500, START + M): (100.0, 2),  # absent at START + 2 m; count 0 at START + 3 m
    }  # fmt: skip


async def test_grouped_rate_reads_an_absent_term_in_an_interior_bucket_as_zero():
    def terms(*pairs):
        return {"sum_other_doc_count": 0, "buckets": [{"key": k, "doc_count": n} for k, n in pairs]}

    buckets = [tb(START, 12, by_status=terms((200, 10), (500, 2))),
               tb(START + M, 6, by_status=terms((200, 6)))]  # fmt: skip
    res = await FakeEs(search=search_response(buckets)).source().fetch(BY_STATUS_RATE, RNG, M)
    s500 = series_id("es", {"http.response.status_code": "500"})
    got = {(r["series_id"], r["ts_ms"]): r["avg"] for r in rows(res)}
    assert got[(s500, START + M)] == pytest.approx(2 / 60)
    assert got[(s500, START + 2 * M)] == 0.0


async def test_truncated_terms_are_refused_not_silently_dropped():
    buckets = [tb(START, 12, by_status={"sum_other_doc_count": 3,
                                        "buckets": [{"key": 200, "doc_count": 9}]})]  # fmt: skip
    with pytest.raises(LimitExceeded, match="other terms") as e:
        await FakeEs(search=search_response(buckets)).source().fetch(BY_STATUS_RATE, RNG, M)
    assert "size" in e.value.hint


async def test_more_series_than_the_limit_is_refused():
    buckets = [tb(START, 3, by_status={"sum_other_doc_count": 0, "buckets": [
        {"key": 200, "doc_count": 2}, {"key": 500, "doc_count": 1}]})]  # fmt: skip
    src = FakeEs(search=search_response(buckets)).source(limits=Limits(max_series=1))
    with pytest.raises(LimitExceeded, match="2 series"):
        await src.fetch(BY_STATUS_RATE, RNG, M)


async def test_too_many_steps_are_refused_before_sending():
    fake = FakeEs()
    with pytest.raises(LimitExceeded, match="steps exceeds"):
        await fake.source().fetch(RATE, TimeRange(START, START + 12_000_000), 1_000)
    assert fake.requests == []


async def test_a_server_side_timeout_keeps_the_data_and_marks_the_span_partial():
    resp = search_response(RATE_BUCKETS, timed_out=True)
    res = await FakeEs(search=resp).source().fetch(RATE, RNG, M)
    assert len(rows(res)) == 3
    [(a, b, why)] = res.failed
    assert (a, b) == (RNG.start_ms, RNG.end_ms) and why.startswith("PartialResponse:")
    assert "timed out" in why


async def test_shard_failures_mark_the_span_partial_with_the_reason():
    failures = [{"shard": 0, "index": "tn-access-1",
                 "reason": {"type": "node_disconnected_exception", "reason": "node left"}}]  # fmt: skip
    resp = search_response(RATE_BUCKETS, total=2, failed=1, failures=failures)
    res = await FakeEs(search=resp).source().fetch(RATE, RNG, M)
    assert "1 of 2 shards failed" in res.failed[0][2]
    assert "node_disconnected_exception" in res.failed[0][2]


async def test_a_wildcard_matching_no_index_answers_200_with_zero_shards():
    resp = search_response([], total=0)
    with pytest.raises(SourceError, match="matches no index"):
        await FakeEs(search=resp).source().fetch(RATE, RNG, M)


async def test_an_unmapped_aggregated_field_is_refused_before_searching():
    fake = FakeEs(caps={k: v for k, v in CAPS.items() if k != "event.duration"})
    with pytest.raises(SourceError, match=f"event.duration is not in the mapping of {PATTERN}"):
        await fake.source().fetch(STATS, RNG, M)
    assert fake.searches() == []


async def test_a_metric_on_a_non_numeric_field_is_refused():
    expr = json.dumps({"aggs": {"lat": {"stats": {"field": "service.name"}}}})
    with pytest.raises(SourceError, match="service.name is keyword, not numeric"):
        await FakeEs().source().fetch(expr, RNG, M)


async def test_terms_on_a_text_field_is_refused_with_the_keyword_hint():
    expr = json.dumps({"aggs": {"by_msg": {"terms": {"field": "message"}}}})
    with pytest.raises(SourceError, match="not aggregatable") as e:
        await FakeEs().source().fetch(expr, RNG, M)
    assert "message.keyword" in e.value.hint


async def test_a_field_mapped_differently_across_indices_is_refused():
    caps = {**CAPS, "event.duration": {"long": {"type": "long", "aggregatable": True},
                                       "keyword": {"type": "keyword", "aggregatable": True}}}  # fmt: skip
    with pytest.raises(SourceError, match="mapped as keyword, long"):
        await FakeEs(caps=caps).source().fetch(STATS, RNG, M)


async def test_field_checks_are_cached_per_source():
    fake = FakeEs(search=search_response([]))
    src = fake.source()
    await src.fetch(STATS, RNG, M)
    await src.fetch(STATS, RNG, M)
    assert fake.caps_requests() == ["event.duration"]


async def test_an_unsupported_shape_lists_the_accepted_forms():
    with pytest.raises(SourceError, match="avg is not supported") as e:
        await FakeEs().source().fetch(json.dumps({"aggs": {"a": {"avg": {"field": "x"}}}}), RNG, M)
    assert "value_count" in e.value.hint


async def test_a_histogram_belongs_to_query_distribution():
    with pytest.raises(SourceError, match="distribution") as e:
        await FakeEs().source().fetch_values(HIST, RNG, M)
    assert "query_distribution" in e.value.hint


TOO_MANY = {
    "type": "too_many_buckets_exception",
    "reason": "Trying to create too many buckets. Must be less than or equal to: [65536]",
}
FIELDDATA = (
    "Text fields are not optimised for operations that require per-document field data "
    "like aggregations and sorting, so these operations are disabled by default. Please "
    "use a keyword field instead. Alternatively, set fielddata=true on [message]"
)


@pytest.mark.parametrize(
    ("response", "error", "match"),
    [
        (es_error(400, "parsing_exception", "unknown query [query_strin]"), SourceError,
         "query failed: parsing_exception: unknown query"),
        (es_error(400, "x_content_parse_exception", "[1:10] unknown field [qury]"), SourceError,
         "x_content_parse_exception"),
        (es_error(400, "search_phase_execution_exception", "all shards failed",
                  root=("query_shard_exception", "failed to create query: For input string")),
         SourceError, "query_shard_exception: failed to create query"),
        (es_error(400, "illegal_argument_exception", FIELDDATA), SourceError, "text field"),
        (es_error(503, "search_phase_execution_exception", "all shards failed", caused_by=TOO_MANY),
         LimitExceeded, "too many buckets"),
        (es_error(400, "too_many_buckets_exception", TOO_MANY["reason"]), LimitExceeded,
         "too many buckets"),
        (es_error(429, "circuit_breaking_exception", "[parent] Data too large"), LimitExceeded,
         "too much memory"),
        (es_error(429, "es_rejected_execution_exception", "rejected execution of coordinating"),
         SourceUnavailable, "HTTP 429"),
        (es_error(500, "null_pointer_exception", "boom"), SourceUnavailable, "HTTP 500"),
        (es_error(403, "security_exception", "action [indices:data/read/search] is unauthorized"),
         _Forbidden, "permission denied: security_exception"),
        (es_error(404, "index_not_found_exception", "no such index [tn-access-x]"), SourceError,
         "matches no index"),
        (httpx.Response(200, text="<html>Sign in</html>"), SourceUnavailable, "non-JSON"),
    ],
)  # fmt: skip
async def test_search_errors_map_to_typed_source_errors(response, error, match):
    with pytest.raises(SourceError, match=match) as e:
        await FakeEs(search=response).source().fetch(RATE, RNG, M)
    assert type(e.value) is error
    assert e.value.hint
