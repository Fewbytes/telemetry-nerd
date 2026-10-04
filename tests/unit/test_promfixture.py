"""The e2e fixture PromQL source (bead y7hb): engine semantics and the daemon's adapter on it."""

import math

import httpx
import pytest

from telemetry_nerd.devtools.promfixture.dataset import seed
from telemetry_nerd.devtools.promfixture.engine import Engine, EvalError, Unsupported
from telemetry_nerd.devtools.promfixture.server import create_app
from telemetry_nerd.devtools.promfixture.store import Store
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.observed import observed_count_query
from telemetry_nerd.sources.promql import PromQLSource

T0 = 1_800_000_000_000  # a multiple of 15s and of 1h
S = 15_000


def _store(**series) -> Store:
    st = Store()
    for text in series.values():
        st.import_text(text, T0)
    return st


def _counter(name: str, labels: str, per_scrape: float, n: int = 41) -> str:
    return "".join(f"{name}{{{labels}}} {per_scrape * i} {T0 + i * S}\n" for i in range(n))


def test_rate_of_a_steady_counter_is_its_slope_with_prometheus_extrapolation():
    eng = Engine(_store(a=_counter("req_total", 'i="a"', 15.0)))
    kind, vec = eng.instant("rate(req_total[1m])", T0 + 20 * S)
    assert kind == "vector"
    ((labels, v),) = vec
    assert labels == {"i": "a"}  # rate drops the metric name
    assert v == pytest.approx(1.0)
    _, inc = eng.instant("increase(req_total[1m])", T0 + 20 * S)
    assert inc[0][1] == pytest.approx(60.0)


def test_counter_reset_is_compensated():
    text = "".join(f"c {v} {T0 + i * S}\n" for i, v in enumerate([10, 20, 30, 5, 15]))
    _, vec = Engine(_store(a=text)).instant("increase(c[2m])", T0 + 4 * S)
    # raw 5 + 30 (the reset), extrapolated by half a scrape at the start (the zero point is
    # further than 1.1 scrapes away): 35 * 67.5 / 60
    assert vec[0][1] == pytest.approx(39.375)


def test_instant_selector_looks_back_five_minutes_left_open():
    eng = Engine(_store(a=f"g 1 {T0}\n"))
    assert eng.instant("g", T0 + 299_999)[1]
    assert eng.instant("g", T0 + 300_000)[1] == []
    assert eng.instant("g", T0 - 1)[1] == []


def test_range_window_is_left_open():
    eng = Engine(_store(a="".join(f"g {i} {T0 + i * S}\n" for i in range(9))))
    _, vec = eng.instant("count_over_time(g[1m])", T0 + 8 * S)
    assert vec[0][1] == 4.0  # (T0+60s, T0+120s]: samples 5..8
    _, m = eng.instant("g[1m]", T0 + 8 * S)
    assert [t for t in m[0][1]] == [T0 + i * S for i in range(5, 9)]


def test_aggregations_group_by_and_without():
    text = "".join(
        f'm{{job="{j}",i="{i}"}} {v} {T0}\n'
        for j, i, v in [("x", "1", 1), ("x", "2", 3), ("y", "1", 10)]
    )
    eng = Engine(_store(a=text))
    by = {lb["job"]: v for lb, v in eng.instant("sum by (job) (m)", T0)[1]}
    assert by == {"x": 4.0, "y": 10.0}
    wo = {tuple(sorted(lb.items())): v for lb, v in eng.instant("max without (i) (m)", T0)[1]}
    assert wo == {(("job", "x"),): 3.0, (("job", "y"),): 10.0}
    assert eng.instant("count(m)", T0)[1] == [({}, 3.0)]
    top = eng.instant("topk(1, m)", T0)[1]
    assert top == [({"__name__": "m", "job": "y", "i": "1"}, 10.0)]


def test_histogram_quantile_interpolates_within_the_bucket():
    text = "".join(
        f'h_bucket{{le="{le}"}} {c} {T0}\n' for le, c in [("1", 50), ("2", 100), ("+Inf", 100)]
    )
    _, vec = Engine(_store(a=text)).instant("histogram_quantile(0.75, h_bucket)", T0)
    assert vec == [({}, 1.5)]


def test_subquery_points_are_aligned_to_multiples_of_the_step():
    eng = Engine(_store(a="".join(f"g {i} {T0 + i * S}\n" for i in range(20))))
    _, m = eng.instant("(g * 2)[1m:30s]", T0 + 10 * S + 5_000)
    # (t - 1m, t] = (T0+95s, T0+155s]: the 30s multiples in it
    assert m[0][1] == [T0 + 120_000, T0 + 150_000]
    assert m[0][2] == [16.0, 20.0]


def test_vector_matching_one_to_one_on_and_group_left():
    text = (
        f'a{{job="x",i="1"}} 6 {T0}\na{{job="x",i="2"}} 8 {T0}\n'
        f'b{{job="x"}} 2 {T0}\nb{{job="z"}} 5 {T0}\n'
    )
    eng = Engine(_store(a=text))
    out = eng.instant("a / on(job) group_left b", T0)[1]
    assert sorted((lb["i"], v) for lb, v in out) == [("1", 3.0), ("2", 4.0)]
    with pytest.raises(EvalError):
        eng.instant("a / on(job) b", T0)  # many-to-one must be explicit
    assert eng.instant("a > 7", T0)[1] == [({"__name__": "a", "job": "x", "i": "2"}, 8.0)]
    assert sorted(v for _, v in eng.instant("a > bool 7", T0)[1]) == [0.0, 1.0]


def test_the_daemons_observed_count_min_fold_evaluates():
    text = _counter("a_total", 'i="1"', 1) + _counter("b_total", 'i="1"', 1, n=30)
    q = observed_count_query("sum(rate(a_total[1m])) / sum(rate(b_total[1m]))", "1m")
    assert q is not None and " or " in q and " and " in q
    out = Engine(_store(a=text)).range(q, T0 + 20 * S, T0 + 40 * S, 60_000)
    assert out and all(v <= 4 for v in out[0][2])


def test_unsupported_constructs_fail_loudly():
    eng = Engine(_store(a=f"g 1 {T0}\n"))
    with pytest.raises(Unsupported):
        eng.instant('count_values("v", g)', T0)


def test_range_query_steps_and_scalars():
    eng = Engine(_store(a="".join(f"g {i} {T0 + i * S}\n" for i in range(9))))
    ((labels, ts, vs),) = eng.range("g + 1", T0, T0 + 8 * S, 2 * S)
    assert labels == {} and ts == [T0 + k * 2 * S for k in range(5)]
    assert vs == [1.0, 3.0, 5.0, 7.0, 9.0]
    ((_, _, vs),) = eng.range("time()", T0, T0 + S, S)
    assert vs == [T0 / 1000, (T0 + S) / 1000]


# --- the daemon's adapter against the fixture app ---------------------------------------------


@pytest.fixture
async def source():
    store = Store()
    anchor = seed(store, T0 + 7_000)
    app = create_app(store, clock=lambda: anchor)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fx")
    src = PromQLSource("default", "http://fx", flavor="prometheus", client=client)
    yield src, anchor
    await client.aclose()


async def test_adapter_fetches_buckets_and_learns_the_resolution(source):
    src, anchor = source
    info = await src.learn_resolution()
    assert info["resolution"] == "15s" and info["origin"] == "learned"
    rng = TimeRange(anchor - 3 * 3_600_000, anchor - 600_000)
    res = await src.fetch("tn_demo_latency_seconds", rng, 60_000)
    assert res.series.num_rows == 3
    assert res.buckets.num_rows == 3 * 171
    peak = max(res.buckets.column("max").to_pylist())
    assert peak == 1.5  # c's planted spike
    counts = set(res.buckets.column("count").to_pylist())
    assert counts == {4}  # 4 samples per 1m bucket at 15s
    rate = await src.fetch("sum(rate(tn_demo_requests_total[5m]))", rng, 60_000)
    assert rate.series.num_rows == 1


async def test_adapter_discovery_histogram_and_import(source):
    src, anchor = source
    disc = await src.discover()
    names = {m.name for m in disc.metrics}
    assert {"tn_demo_latency_seconds", "tn_demo_holes_seconds", "up"} <= names
    rng = TimeRange(anchor - 3_600_000, anchor - 600_000)
    dist = await src.fetch_histogram(
        "tn_demo_request_duration_seconds_bucket", ["instance"], rng, 300_000
    )
    assert dist.series.num_rows == 3
    # the VictoriaMetrics import API: specs add their own series per test
    resp = await src._client.post(
        "http://fx/api/v1/import/prometheus", content=f'tn_e2e_x{{k="v"}} 4 {anchor - 60_000}\n'
    )
    assert resp.status_code == 204
    assert await src.label_values("k", ["tn_e2e_x"]) == ["v"]
    bad = await src._client.get("http://fx/api/v1/query", params={"query": 'count_values("v", up)'})
    assert bad.status_code == 422 and "does not support" in bad.json()["error"]


def test_seeded_data_is_a_function_of_time_since_the_anchor():
    def relative(now):
        st = Store()
        anchor = seed(st, now)
        eng = Engine(st)
        rows = eng.range(
            "avg_over_time(tn_demo_latency_seconds[1m])", anchor - 3 * 3_600_000, anchor, 60_000
        )
        return sorted(
            (tuple(sorted(lb.items())), [t - anchor for t in ts], vs) for lb, ts, vs in rows
        )

    a = relative(T0 + 3_000)
    b = relative(T0 + 5 * 3_600_000 + 47 * S)
    assert a == b
    assert all(not math.isnan(v) for _, _, vs in a for v in vs)


def test_extrapolation_threshold_applies_before_the_counter_zero_cap():
    # Prometheus 3 extrapolatedRate: a start gap >= 1.1 average spacings is first cut to half a
    # spacing, and only then capped by the counter's zero point. Here the gap is 60s (avg 15s ->
    # 7.5s) and the zero point is 10s back: 7.5s wins. (Prometheus 2 capped first: 10s.)
    text = "".join(f"c {v} {T0 + i * S}\n" for i, v in enumerate([10, 25, 40, 55, 70]))
    _, vec = Engine(_store(a=text)).instant("increase(c[2m])", T0 + 4 * S)
    assert vec[0][1] == pytest.approx(60 * 67.5 / 60)


async def test_samples_after_now_are_not_visible_yet(source):
    # `up` is generated past the anchor so it keeps "arriving" while the suite runs; a sample is
    # visible only once the server's clock has reached it (no future data in reads or listings)
    src, anchor = source
    client = src._client
    await client.post(
        "http://fx/api/v1/import/prometheus", content=f"tn_e2e_future 1 {anchor + 60_000}\n"
    )
    names = (await client.get("http://fx/api/v1/label/__name__/values")).json()["data"]
    assert "tn_e2e_future" not in names and "up" in names
    series = (
        await client.get("http://fx/api/v1/series", params={"match[]": "tn_e2e_future"})
    ).json()
    assert series["data"] == []
    later = {"query": "up[10m]", "time": f"{(anchor + 60_000) / 1000}"}
    (row,) = (await client.get("http://fx/api/v1/query", params=later)).json()["data"]["result"]
    assert float(row["values"][-1][0]) * 1000 == anchor  # the samples after it are not in yet


def test_anchor_is_a_whole_minute_so_one_minute_buckets_cut_the_data_the_same_way_every_run():
    """bead ax1s: the daemon aligns ranges to whole steps of absolute time; an anchor on the 15s
    grid but off the minute put the 1m buckets over different samples depending on the second
    the fixture started (the spc spec saw 0 or 7 violations by that alone)."""
    for offset in (0, 7_000, 15_000, 31_000, 59_999):
        st = Store()
        assert seed(st, T0 + offset) == T0
