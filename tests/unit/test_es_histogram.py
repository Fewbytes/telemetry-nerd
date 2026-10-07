"""ElasticsearchSource.fetch_histogram: query-chosen value buckets mapped onto DistResult."""

import json

import pytest

from telemetry_nerd.model.distribution import BucketScheme
from telemetry_nerd.model.series import series_id
from telemetry_nerd.sources.base import LimitExceeded, SourceError

from .es_fake import RNG, START, FakeEs, search_response, tb

M = 60_000
SELECTOR = json.dumps({"query": {"query_string": {"query": "NOT url.path:\"/healthz\""}},
                       "aggs": {"lat": {"histogram": {"field": "event.duration", "interval": 25}}}})  # fmt: skip


def hist(*pairs):
    return {"buckets": [{"key": k, "doc_count": n} for k, n in pairs]}


async def test_ungrouped_histogram_rows_columns_and_scheme():
    buckets = [
        tb(START - M, 0, lat=hist()),
        tb(START, 13, lat=hist((0.0, 10), (25.0, 0), (50.0, 3))),
        tb(START + M, 0, lat=hist()),
        tb(START + 2 * M, 4, lat=hist((25.0, 4))),
    ]
    dist = (
        await FakeEs(search=search_response(buckets)).source().fetch_histogram(SELECTOR, (), RNG, M)
    )
    sid = series_id("es", {})
    assert dist.rows.to_pylist() == [
        {"ts_ms": START + M, "series_id": sid, "bucket_lo": 0.0, "bucket_hi": 25.0, "count": 10.0},
        {"ts_ms": START + M, "series_id": sid, "bucket_lo": 50.0, "bucket_hi": 75.0, "count": 3.0},
        {
            "ts_ms": START + 3 * M,
            "series_id": sid,
            "bucket_lo": 25.0,
            "bucket_hi": 50.0,
            "count": 4.0,
        },
    ]  # zero-count leaf buckets are not rows
    assert dist.columns.to_pylist() == [
        {"ts_ms": START + M, "series_id": sid, "n": 13.0},
        {"ts_ms": START + 2 * M, "series_id": sid, "n": 0.0},  # interior: zero documents
        {"ts_ms": START + 3 * M, "series_id": sid, "n": 4.0},
    ]
    assert dist.scheme == BucketScheme("linear", width=25.0, offset=0.0)
    assert dist.expr == SELECTOR and dist.caveats == ("query_chosen_buckets",)


async def test_by_builds_one_terms_level_and_fills_absent_groups_with_n_zero():
    def by(*groups):
        return {"sum_other_doc_count": 0,
                "buckets": [{"key": k, "doc_count": sum(n for _, n in h), "lat": hist(*h)}
                            for k, h in groups]}  # fmt: skip

    buckets = [
        tb(START, 15, __tn_by=by(("checkout", [(25.0, 10)]), ("search", [(0.0, 5)]))),
        tb(START + M, 10, __tn_by=by(("checkout", [(25.0, 10)]))),
    ]
    fake = FakeEs(search=search_response(buckets))
    dist = await fake.source().fetch_histogram(SELECTOR, ["service.name"], RNG, M)
    search = series_id("es", {"service.name": "search"})
    cols = {(c["series_id"], c["ts_ms"]): c["n"] for c in dist.columns.to_pylist()}
    assert cols[(search, START + M)] == 5.0 and cols[(search, START + 2 * M)] == 0.0
    inner = fake.searches()[0]["aggs"]["__tn_time"]["aggs"]
    assert inner["__tn_by"]["terms"] == {"field": "service.name", "size": 501}
    assert inner["__tn_by"]["aggs"] == {
        "lat": {"histogram": {"field": "event.duration", "interval": 25}}
    }
    assert {json.loads(r["labels"])["service.name"] for r in dist.series.to_pylist()} == {
        "checkout",
        "search",
    }


async def test_the_scheme_carries_the_histogram_offset():
    sel = json.dumps({"aggs": {"lat": {"histogram": {"field": "event.duration", "interval": 10,
                                                     "offset": 5}}}})  # fmt: skip
    buckets = [tb(START, 2, lat=hist((5.0, 2)))]
    dist = await FakeEs(search=search_response(buckets)).source().fetch_histogram(sel, (), RNG, M)
    assert dist.scheme == BucketScheme("linear", width=10.0, offset=5.0)
    assert dist.rows.to_pylist()[0]["bucket_hi"] == 15.0


async def test_more_than_one_by_field_is_refused():
    with pytest.raises(SourceError, match="at most one field"):
        await FakeEs().source().fetch_histogram(SELECTOR, ["a", "b"], RNG, M)


async def test_a_selector_that_is_not_a_histogram_is_refused():
    stats = json.dumps({"aggs": {"lat": {"stats": {"field": "event.duration"}}}})
    with pytest.raises(SourceError, match="needs a histogram aggregation"):
        await FakeEs().source().fetch_histogram(stats, (), RNG, M)


async def test_truncated_groups_are_refused():
    buckets = [tb(START, 9, __tn_by={"sum_other_doc_count": 4, "buckets": []})]
    with pytest.raises(LimitExceeded, match="other terms"):
        await (
            FakeEs(search=search_response(buckets))
            .source()
            .fetch_histogram(SELECTOR, ["service.name"], RNG, M)
        )


async def test_a_partial_response_marks_the_distribution_span():
    buckets = [tb(START, 1, lat=hist((0.0, 1)))]
    resp = search_response(buckets, timed_out=True)
    dist = await FakeEs(search=resp).source().fetch_histogram(SELECTOR, (), RNG, M)
    assert dist.failed and dist.failed[0][2].startswith("PartialResponse:")
