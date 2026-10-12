"""Concurrency derived from access-log documents (telemetry-nerd-sgb.11): the pure sweep, the
in_flight expr form, the adapter's bounded scan and its use in check_littles_law."""

import asyncio
import json

import numpy as np
import pytest

from telemetry_nerd.sources import inflight
from telemetry_nerd.sources.base import LimitExceeded, SourceError
from telemetry_nerd.sources.esquery import EsQuery

from .es_fake import RNG, START, FakeEs, search_response, tb
from .fakes import NOW, make_service
from .littles_sim import EsSimSource, SimSource, simulate

S = 1000
M = 60_000


def _brute(start, end, grid, step, res=1.0):
    """Time average, min and max of the number in flight by evaluating it every `res` ms."""
    out = []
    for t in grid:
        u = np.arange(t - step, t, res) + res / 2
        n = ((start[:, None] <= u) & (u < end[:, None])).sum(axis=0)
        out.append((n.mean(), n.min(), n.max()))
    return np.array(out)


# pure sweep ---------------------------------------------------------------------------------


def test_derive_is_the_exact_time_average_with_min_and_max():
    start = np.array([500.0, 200.0, 1200.0])
    end = np.array([1500.0, 2700.0, 1300.0])
    grid = np.array([1000, 2000, 3000], np.int64)
    got = inflight.derive(start, end, grid, S)
    # [0,1000): 0.8 s of one + 0.5 s of another = 1.3; [1000,2000): 1 + 0.5 + 0.1 = 1.6;
    # [2000,3000): 0.7
    np.testing.assert_allclose(got.avg, [1.3, 1.6, 0.7])
    np.testing.assert_array_equal(got.min, [0, 1, 0])
    np.testing.assert_array_equal(got.max, [2, 3, 1])
    np.testing.assert_array_equal(got.ends, grid)


def test_derive_matches_brute_force_on_random_requests():
    rng = np.random.default_rng(7)
    start = rng.uniform(0, 20_000, 300)
    end = start + rng.exponential(800, 300)
    grid = np.arange(1000, 20_001, S, dtype=np.int64)
    got = inflight.derive(start, end, grid, S)
    ref = _brute(start, end, got.ends, S, res=0.25)
    np.testing.assert_allclose(got.avg, ref[:, 0], atol=0.02)
    np.testing.assert_array_equal(got.min, ref[:, 1])
    np.testing.assert_array_equal(got.max, ref[:, 2])


def test_derive_at_epoch_scale_keeps_precision():
    base = 1_700_000_000_000
    start = base + np.array([500.0, 200.0, 1200.0])
    end = base + np.array([1500.0, 2700.0, 1300.0])
    got = inflight.derive(start, end, base + np.array([1000, 2000, 3000], np.int64), S)
    np.testing.assert_allclose(got.avg, [1.3, 1.6, 0.7], atol=1e-9)


def test_buckets_outside_the_documents_coverage_are_left_out():
    got = inflight.derive(np.array([2500.0]), np.array([3500.0]), np.arange(1000, 6001, S), S)
    np.testing.assert_array_equal(got.ends, [3000, 4000])  # unknown before/after, not zero


def test_spans_follow_the_timestamp_semantics():
    ts, d = np.array([10_000.0]), np.array([2.0])
    assert [x[0] for x in inflight.spans(ts, d, 1e3, "start")] == [10_000, 12_000]
    assert [x[0] for x in inflight.spans(ts, d, 1e3, "end")] == [8_000, 10_000]


def test_read_range_reaches_out_on_the_side_requests_come_from():
    assert inflight.read_range(10 * M, 20 * M, M, 5 * M, "start") == (4 * M, 20 * M)
    assert inflight.read_range(10 * M, 20 * M, M, 5 * M, "end") == (9 * M, 25 * M)


def test_plan_slices_packs_buckets_under_the_cap():
    buckets = [(0, 4), (10, 4), (20, 3), (30, 0), (40, 9)]
    assert inflight.plan_slices(buckets, 10, cap=9) == [(0, 20, 8), (20, 40, 3), (40, 50, 9)]
    with pytest.raises(ValueError, match="one 10 ms bucket"):
        inflight.plan_slices([(0, 10)], 10, cap=9)


# the expr form ------------------------------------------------------------------------------

QS = {"query_string": {"query": "service.name:checkout"}}


def _form(**params):
    body = {"field": "event.duration", "unit": "ns", "timestamp": "end", **params}
    return json.dumps({"query": QS, "aggs": {"l": {"in_flight": body}}})


def test_in_flight_form_parses_with_explicit_unit_and_timestamp():
    q = EsQuery.parse(_form())
    assert (q.form, q.field, q.unit_ms, q.timestamp) == ("in_flight", "event.duration", 1e-6, "end")
    assert q.max_duration_ms == 5 * M  # default, stated in the result's notes
    assert EsQuery.parse(_form(max_duration="30s")).max_duration_ms == 30_000


@pytest.mark.parametrize(
    "params, match",
    [({"unit": None}, "unit"), ({"timestamp": "middle"}, "timestamp"),
     ({"max_duration": "soon"}, "max_duration"), ({"bucket": 1}, "not \\['bucket'\\]")],
)  # fmt: skip
def test_in_flight_form_refuses_guesses(params, match):
    body = {k: v for k, v in {"field": "event.duration", "unit": "ns", "timestamp": "end",
                              **params}.items() if v is not None}  # fmt: skip
    with pytest.raises(SourceError, match=match):
        EsQuery.parse(json.dumps({"aggs": {"l": {"in_flight": body}}}))


def test_in_flight_takes_no_grouping():
    expr = {"aggs": {"t": {"terms": {"field": "host.name"}, "aggs": {"l": {"in_flight": {
        "field": "event.duration", "unit": "ns", "timestamp": "end"}}}}}}  # fmt: skip
    with pytest.raises(SourceError, match="no terms grouping"):
        EsQuery.parse(json.dumps(expr))


# the adapter's scan -------------------------------------------------------------------------


def _docs(pairs):
    """Hits as docvalue_fields return them: (timestamp ms, duration ns)."""
    return [{"fields": {"@timestamp": [str(t)], "event.duration": [d]}} for t, d in pairs]


def _cluster(pairs, page_extra=0):
    """A FakeEs answering in_flight's count request and slice requests from `pairs`."""

    def search(body):
        rng = body["query"]["bool"]["filter"][1]["range"]["@timestamp"]
        inside = [(t, d) for t, d in pairs if rng["gte"] <= t < rng["lt"]]
        if body.get("size", 0) == 0:
            hist = body["aggs"]["__tn_time"]["date_histogram"]
            step = int(hist["fixed_interval"][:-2])
            keys = range(rng["gte"], rng["lt"], step)
            return search_response(
                [tb(k, sum(1 for t, _ in inside if k <= t < k + step)) for k in keys]
            )
        resp = search_response([])
        resp["hits"]["hits"] = _docs(inside) + _docs(inside[:1]) * page_extra
        return resp

    return FakeEs(search=search)


# requests timestamped at their END, durations in ns
PAIRS = [(START + 30_000, 20_000 * 10**6), (START + 90_000, 90_000 * 10**6),
         (START + 150_000, 30_000 * 10**6)]  # fmt: skip


async def test_fetch_derives_concurrency_from_documents():
    fake = _cluster(PAIRS)
    res = await fake.source().fetch(_form(max_duration="2m"), RNG, M)
    rows = {r["ts_ms"]: r for r in res.buckets.to_pylist()}
    # spans: [START+10s, START+30s), [START+0s, START+90s), [START+120s, START+150s)
    assert rows[START + M]["avg"] == pytest.approx((20 + 60) / 60)
    assert rows[START + 2 * M]["avg"] == pytest.approx(30 / 60)
    assert rows[START + 3 * M]["avg"] == pytest.approx(30 / 60)
    assert rows[START + M]["max"] == 2
    assert not res.failed
    [note] = res.notes
    assert "3 document(s)" in note and "timestamp = end" in note and "ASSUMED" in note
    count_body, *slices = fake.searches()
    assert count_body["size"] == 0
    assert all(b["size"] == inflight.PAGE and b["_source"] is False for b in slices)
    # end timestamps: read up to max_duration past the range end
    assert (
        count_body["query"]["bool"]["filter"][1]["range"]["@timestamp"]["lt"] == RNG.end_ms + 2 * M
    )


async def test_requests_longer_than_max_duration_mark_an_undercount_span():
    fake = _cluster(PAIRS)
    res = await fake.source().fetch(_form(max_duration="30s"), RNG, M)
    [(lo, hi, why)] = res.failed
    assert why.startswith("Undercount") and "90s" in why
    assert (lo, hi) == (RNG.end_ms - 60_000, RNG.end_ms)  # end side: 90 s seen - 30 s read


async def test_documents_without_a_duration_are_counted_as_missing():
    pairs = [*PAIRS, (START + 100_000, None)]
    res = await _cluster(pairs).source().fetch(_form(max_duration="2m"), RNG, M)
    assert any("1 document(s) without a usable duration" in n for n in res.notes)


async def test_more_documents_than_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(inflight, "MAX_DOCS", 2)
    with pytest.raises(LimitExceeded, match="reads every document: 3"):
        await _cluster(PAIRS).source().fetch(_form(max_duration="2m"), RNG, M)


async def test_a_full_page_is_refused_not_truncated(monkeypatch):
    monkeypatch.setattr(inflight, "PAGE", 4)
    with pytest.raises(LimitExceeded, match="full page"):
        await _cluster(PAIRS, page_extra=4).source().fetch(_form(max_duration="2m"), RNG, M)


# check_littles_law --------------------------------------------------------------------------

LOGS = json.dumps({"query": QS, "aggs": {"l": {"in_flight": {
    "field": "event.duration", "unit": "s", "timestamp": "start"}}}})  # fmt: skip
ARRIVALS = json.dumps({"query": QS})
LATENCY = json.dumps({"query": QS, "aggs": {"lat": {"stats": {"field": "event.duration"}}}})


def _setup(tmp_path):
    sims = {"i0": simulate(20, rates=[(0.0, 6.0)], c=10)}
    start = NOW - 3_600_000
    prom = SimSource(sims, start)
    logs = EsSimSource(sims, start)
    logs.name, logs.identity = "logs", "logs-sim"
    svc = make_service(tmp_path, source=prom)
    svc.sources.attach("logs", logs)
    return svc


def _check(svc, **kw):
    args = {"arrival_rate": "http_requests_total", "latency": "http_request_duration_seconds",
            "concurrency": LOGS, "concurrency_source": "logs", "latency_unit": "s"}  # fmt: skip
    return asyncio.run(
        svc.check_littles_law(start="now-1h", end="now", window="5m", **{**args, **kw})
    )


def test_derived_concurrency_from_another_source_is_consistent_and_stated(tmp_path):
    out = _check(_setup(tmp_path))
    assert out["verdict"] == "consistent"
    a = {x["name"]: x for x in out["assumptions"]}
    assert a["concurrency_derived"]["status"] == "assumed"
    assert "timestamp = start" in a["concurrency_derived"]["detail"]
    assert "independent" in a["concurrency_derived"]["detail"]
    assert "exact per sub-step" in a["gauge_sampling"]["detail"]


def test_derived_concurrency_from_the_signals_own_documents_is_refused(tmp_path):
    svc = _setup(tmp_path)
    with pytest.raises(ValueError, match="circular"):
        _check(svc, source="logs", arrival_rate=ARRIVALS, latency=LATENCY)
