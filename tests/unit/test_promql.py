import math

import httpx
import pytest
import respx

from telemetry_nerd.model.series import series_id
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.promql import USER_AGENT, PromQLSource, is_selector
from telemetry_nerd.sources.spec import AuthRef, MissingSecret, SourceSpec

BASE = "http://vm.test"
RNG = TimeRange(1_700_000_040_000, 1_700_000_160_000)
ROUTE = {"host": "vm.test", "path": "/api/v1/query_range"}


def matrix(result):
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def test_is_selector():
    assert is_selector("up")
    assert is_selector('http_requests_total{job="api", code=~"5.."}')
    assert not is_selector("rate(x[1m])")
    assert not is_selector("sum(up)")


def test_build_queries_vm_selector_uses_raw_window():
    src = PromQLSource("vm", BASE)
    assert src.build_queries("up", 60_000) == {
        "rollup": "rollup(up[1m])",
        "count": "count_over_time(up[1m])",
    }


def test_build_queries_vm_expression_uses_subquery_for_values_and_selector_for_count():
    src = PromQLSource("vm", BASE, resolution_ms=15_000)
    q = src.build_queries("sum(rate(x[1m]))", 60_000)
    assert q["rollup"] == "rollup((sum(rate(x[1m])))[1m:15s])"
    # a subquery's count is lookback-filled evaluations, not samples (1h9.11)
    assert q["count"] == "sum (count_over_time(x[1m]))"


def test_build_queries_expression_without_derivable_count_keeps_the_subquery_count():
    src = PromQLSource("prom", BASE, flavor="prometheus", resolution_ms=15_000)
    q = src.build_queries("rate(x[5m]) / on(a) rate(y[5m])", 60_000)
    assert q["count"] == "count_over_time((rate(x[5m]) / on(a) rate(y[5m]))[1m:15s])"
    assert q["avg"] == "avg_over_time((rate(x[5m]) / on(a) rate(y[5m]))[1m:15s])"


def test_build_queries_ratio_counts_fold_to_the_smaller_operand():
    src = PromQLSource("prom", BASE, flavor="prometheus", resolution_ms=15_000)
    q = src.build_queries("rate(x[5m]) / rate(y[5m])", 60_000)
    assert q["count"] == (
        "((count_over_time(x[1m])) <= (count_over_time(y[1m]))) or "
        "((count_over_time(y[1m])) and (count_over_time(x[1m])))"
    )


def test_build_queries_prometheus_flavor():
    src = PromQLSource("prom", BASE, flavor="prometheus")
    assert src.build_queries("up", 60_000) == {
        "avg": "avg_over_time(up[1m])",
        "min": "min_over_time(up[1m])",
        "max": "max_over_time(up[1m])",
        "count": "count_over_time(up[1m])",
    }


@respx.mock
async def test_fetch_pivots_rollup_and_count_into_buckets():
    def responder(request):
        if request.url.params["query"].startswith("rollup("):
            return httpx.Response(
                200,
                json=matrix(
                    [
                        {
                            "metric": {"instance": "a", "rollup": "min"},
                            "values": [[1700000100, "1"], [1700000160, "2"]],
                        },
                        {
                            "metric": {"instance": "a", "rollup": "max"},
                            "values": [[1700000100, "5"], [1700000160, "9"]],
                        },
                        {
                            "metric": {"instance": "a", "rollup": "avg"},
                            "values": [[1700000100, "3"], [1700000160, "4"]],
                        },
                    ]
                ),
            )
        return httpx.Response(
            200,
            json=matrix(
                [
                    {
                        "metric": {"instance": "a"},
                        "values": [[1700000100, "4"], [1700000160, "4"]],
                    }
                ]
            ),
        )

    respx.get(**ROUTE).mock(side_effect=responder)
    res = await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    sid = series_id("vm", {"instance": "a"})
    assert res.buckets.to_pylist() == [
        {
            "ts_ms": 1_700_000_100_000,
            "series_id": sid,
            "avg": 3.0,
            "min": 1.0,
            "max": 5.0,
            "count": 4,
        },
        {
            "ts_ms": 1_700_000_160_000,
            "series_id": sid,
            "avg": 4.0,
            "min": 2.0,
            "max": 9.0,
            "count": 4,
        },
    ]
    assert res.series.to_pylist() == [{"series_id": sid, "labels": '{"instance":"a"}'}]


@respx.mock
async def test_fetch_merges_when_vm_keeps_metric_name():
    """MetricsQL rollup() returns __name__ but count_over_time() drops it;
    both must land on the same series so buckets carry all four fields."""

    def responder(request):
        if request.url.params["query"].startswith("rollup("):
            return httpx.Response(
                200,
                json=matrix(
                    [
                        {
                            "metric": {"__name__": "up", "instance": "a", "rollup": "avg"},
                            "values": [[1700000100, "3"]],
                        },
                        {
                            "metric": {"__name__": "up", "instance": "a", "rollup": "min"},
                            "values": [[1700000100, "1"]],
                        },
                        {
                            "metric": {"__name__": "up", "instance": "a", "rollup": "max"},
                            "values": [[1700000100, "5"]],
                        },
                    ]
                ),
            )
        return httpx.Response(
            200,
            json=matrix([{"metric": {"instance": "a"}, "values": [[1700000100, "4"]]}]),
        )

    respx.get(**ROUTE).mock(side_effect=responder)
    res = await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    sid = series_id("vm", {"instance": "a"})
    assert res.buckets.to_pylist() == [
        {
            "ts_ms": 1_700_000_100_000,
            "series_id": sid,
            "avg": 3.0,
            "min": 1.0,
            "max": 5.0,
            "count": 4,
        }
    ]
    assert res.series.to_pylist() == [{"series_id": sid, "labels": '{"instance":"a"}'}]


@respx.mock
async def test_fetch_sends_step_and_nocache():
    route = respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix([])))
    await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    params = route.calls[0].request.url.params
    assert params["step"] == "60s"
    assert params["nocache"] == "1"
    assert params["start"] == "1700000040.000"


@respx.mock
async def test_series_limit_is_explicit_error():
    result = [
        {"metric": {"i": str(i), "rollup": "avg"}, "values": [[1700000100, "1"]]} for i in range(3)
    ]
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix(result)))
    src = PromQLSource("vm", BASE, limits=Limits(max_series=2))
    with pytest.raises(LimitExceeded) as exc:
        await src.fetch("up", RNG, 60_000)
    assert exc.value.hint and "narrow" in exc.value.hint


async def test_too_many_steps_rejected_before_request():
    src = PromQLSource("vm", BASE)
    with pytest.raises(LimitExceeded) as exc:
        await src.fetch("up", TimeRange(0, 20_000 * 1_000), 1_000)
    assert exc.value.hint and "coarser step" in exc.value.hint


@respx.mock
async def test_query_error_is_surfaced():
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            422, json={"status": "error", "errorType": "bad_data", "error": "parse error at 3"}
        )
    )
    with pytest.raises(SourceError, match="parse error at 3"):
        await PromQLSource("vm", BASE).fetch("up{", RNG, 60_000)


@respx.mock
async def test_connection_failure_is_unavailable():
    respx.get(**ROUTE).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SourceUnavailable) as exc:
        await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert exc.value.hint


def test_identity_includes_flavor_url_and_resolution():
    a = PromQLSource("vm", BASE)
    assert a.identity == f"victoriametrics|{BASE}|15000"
    assert a.identity != PromQLSource("vm", "http://other.test").identity
    assert a.identity != PromQLSource("vm", BASE, flavor="prometheus").identity


def _vm_responder(rollup_ts, count_ts, values=None):
    """rollup query -> min/max/avg at rollup_ts; count query -> 4 at count_ts."""
    values = values or {"min": "1", "max": "5", "avg": "3"}

    def responder(request):
        if request.url.params["query"].startswith("rollup("):
            return httpx.Response(
                200,
                json=matrix(
                    [
                        {
                            "metric": {"instance": "a", "rollup": f},
                            "values": [[t, values[f]] for t in rollup_ts],
                        }
                        for f in ("min", "max", "avg")
                    ]
                ),
            )
        return httpx.Response(
            200,
            json=matrix([{"metric": {"instance": "a"}, "values": [[t, "4"] for t in count_ts]}]),
        )

    return responder


@respx.mock
@pytest.mark.parametrize(
    ("rollup_ts", "count_ts"),
    [([1700000100, 1700000160], [1700000100]), ([1700000100], [1700000100, 1700000160])],
)
async def test_incomplete_cells_are_dropped_and_flagged_partial(rollup_ts, count_ts):
    respx.get(**ROUTE).mock(side_effect=_vm_responder(rollup_ts, count_ts))
    res = await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert res.buckets.column("ts_ms").to_pylist() == [1_700_000_100_000]
    assert res.buckets.column("count").to_pylist() == [4]
    assert res.buckets.column("avg").to_pylist() == [3.0]
    assert res.partial == 1


@respx.mock
async def test_complete_fetch_is_not_partial():
    respx.get(**ROUTE).mock(side_effect=_vm_responder([1700000100], [1700000100]))
    assert (await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)).partial == 0


@respx.mock
@pytest.mark.parametrize("bad", ["NaN", "+Inf", "-Inf"])
async def test_non_finite_values_become_null_but_count_is_kept(bad):
    values = {"min": bad, "max": bad, "avg": bad}
    respx.get(**ROUTE).mock(side_effect=_vm_responder([1700000100], [1700000100], values))
    res = await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    row = res.buckets.to_pylist()[0]
    assert (row["avg"], row["min"], row["max"], row["count"]) == (None, None, None, 4)
    assert res.partial == 0


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_http_5xx_and_429_are_unavailable_with_hint(status):
    respx.get(**ROUTE).mock(return_value=httpx.Response(status, text="overloaded"))
    with pytest.raises(SourceUnavailable) as exc:
        await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert exc.value.hint


@respx.mock
async def test_non_json_body_is_unavailable_with_hint():
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(SourceUnavailable) as exc:
        await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert exc.value.hint


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"status": "success"},
        {"status": "success", "data": {"resultType": "matrix"}},
        {"status": "success", "data": {"result": []}},
        {"status": "success", "data": None},
        {"status": "success", "data": {"resultType": "matrix", "result": [{"values": []}]}},
        {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}}]}},
        {"status": "success", "data": {"resultType": "matrix", "result": ["x"]}},
        [1, 2],
    ],
)
async def test_malformed_success_body_is_source_error_with_hint(body):
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(SourceError) as exc:
        await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert exc.value.hint


@respx.mock
async def test_from_spec_sends_user_agent_and_auth():
    route = respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix([])))
    spec = SourceSpec(name="s", url=BASE, auth=AuthRef(env="TN_T"))
    src = PromQLSource.from_spec(spec, environ={"TN_T": "tok"})
    await src.fetch("up", RNG, 60_000)
    sent = route.calls.last.request.headers
    assert sent["user-agent"] == USER_AGENT
    assert sent["authorization"] == "Bearer tok"
    await src.aclose()


def test_from_spec_missing_secret_raises():
    spec = SourceSpec(name="s", url=BASE, auth=AuthRef(env="TN_NOPE"))
    with pytest.raises(MissingSecret):
        PromQLSource.from_spec(spec, environ={})


def test_from_spec_maps_politeness_and_resolution():
    spec = SourceSpec.model_validate(
        {
            "name": "s",
            "url": BASE,
            "flavor": "victoriametrics",
            "resolution_ms": 20_000,
            "politeness": {"timeout_s": 90},
        }
    )
    src = PromQLSource.from_spec(spec, environ={})
    assert src.flavor == "victoriametrics"
    assert src.resolution_ms == 20_000
    assert src.limits.timeout_s == 90
    assert src.name == "s"


@respx.mock
async def test_probe_reports_buildinfo():
    respx.get(host="vm.test", path="/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"application": "Grafana Mimir", "version": "2.15.0"},
            },
        )
    )
    out = await PromQLSource("s", BASE).probe()
    assert out["reachable"] is True
    assert out["application"] == "Grafana Mimir"
    assert out["version"] == "2.15.0"
    assert isinstance(out["latency_ms"], int)


@respx.mock
async def test_probe_falls_back_to_trivial_query_when_buildinfo_hidden():
    respx.get(host="vm.test", path="/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(404, text="404 page not found")
    )
    q = respx.get(host="vm.test", path="/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "scalar", "result": [0, "1"]}}
        )
    )
    out = await PromQLSource("s", BASE).probe()
    assert out["reachable"] is True
    assert q.calls.last.request.url.params["query"] == "1"


@respx.mock
async def test_probe_unreachable_raises_source_unavailable():
    respx.get(host="vm.test", path="/api/v1/status/buildinfo").mock(
        side_effect=httpx.ConnectError("refused")
    )
    respx.get(host="vm.test", path="/api/v1/query").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SourceUnavailable):
        await PromQLSource("s", BASE).probe()


@respx.mock
async def test_fetch_values_is_a_plain_query_range():
    values = {
        "metric": {"__name__": "x", "region": "a"},
        "values": [[1_700_000_040, "0.25"], [1_700_000_100, "NaN"], [1_700_000_160, "0.5"]],
    }
    # samples were observed in the first two buckets only: the third value is filled
    counts = {"metric": {"region": "a"}, "values": [[1_700_000_040, "3"], [1_700_000_100, "4"]]}

    def responder(request):
        if request.url.params["query"].startswith("sum without (le, vmrange)"):
            return httpx.Response(200, json=matrix([counts]))
        return httpx.Response(200, json=matrix([values]))

    route = respx.get(**ROUTE).mock(side_effect=responder)
    src = PromQLSource("s", BASE, flavor="victoriametrics")
    res = await src.fetch_values("histogram_quantile(0.9, x)", RNG, 60_000)
    assert sorted(c.request.url.params["query"] for c in route.calls) == [
        "histogram_quantile(0.9, x)",
        "sum without (le, vmrange) (count_over_time(x[1m]))",
    ]
    rows = res.buckets.to_pylist()
    assert [(r["avg"], r["min"], r["max"], r["count"]) for r in rows] == [
        (0.25, 0.25, 0.25, 1),
        (None, None, None, 1),
    ]
    assert res.series.to_pylist()[0]["series_id"] == series_id("s", {"region": "a"})


@respx.mock
async def test_fetch_values_without_derivable_count_is_one_plain_query():
    route = respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200, json=matrix([{"metric": {}, "values": [[1_700_000_100, "1"]]}])
        )
    )
    src = PromQLSource("s", BASE, flavor="prometheus")
    res = await src.fetch_values("x / on(a) y", RNG, 60_000)
    assert [c.request.url.params["query"] for c in route.calls] == ["x / on(a) y"]
    assert res.buckets.num_rows == 1


@respx.mock
async def test_fetch_histogram_classic_is_one_query():
    respx.get(host="vm.test", path="/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )
    )  # the le-layout probe: nothing to compare
    route = respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=matrix(
                [
                    {"metric": {"le": "0.1", "region": "a"}, "values": [[1_700_000_100, "9"]]},
                    {"metric": {"le": "+Inf", "region": "a"}, "values": [[1_700_000_100, "10"]]},
                ]
            ),
        )
    )
    src = PromQLSource("s", BASE, flavor="prometheus")
    d = await src.fetch_histogram('lat_bucket{job="x"}', ["region"], RNG, 60_000)
    assert route.call_count == 1
    assert route.calls.last.request.url.params["query"] == (
        'sum by (le, vmrange, region) (increase(lat_bucket{job="x"}[1m]))'
    )
    assert d.scheme.kind == "classic"
    assert [(r["bucket_lo"], r["bucket_hi"], r["count"]) for r in d.rows.to_pylist()] == [
        (-math.inf, 0.1, 9.0),
        (0.1, math.inf, 1.0),
    ]
    assert d.columns.to_pylist() == [
        {"ts_ms": 1_700_000_100_000, "series_id": series_id("s", {"region": "a"}), "n": 10.0}
    ]


async def test_fetch_histogram_refuses_expressions():
    with pytest.raises(SourceError, match="selector"):
        await PromQLSource("s", BASE).fetch_histogram("rate(x_bucket[5m])", [], RNG, 60_000)


@respx.mock
async def test_fetch_histogram_on_a_plain_series_explains():
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200, json=matrix([{"metric": {"job": "x"}, "values": [[1_700_000_100, "3"]]}])
        )
    )
    with pytest.raises(SourceError, match="not a histogram") as e:
        await PromQLSource("s", BASE).fetch_histogram("x_total", [], RNG, 60_000)
    assert "_bucket" in (e.value.hint or "")


@respx.mock
async def test_fetch_histogram_series_limit():
    respx.get(host="vm.test", path="/api/v1/query").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )
    )  # the le-layout probe: nothing to compare
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=matrix(
                [
                    {"metric": {"le": "+Inf", "r": r}, "values": [[1_700_000_100, "1"]]}
                    for r in ("a", "b")
                ]
            ),
        )
    )
    src = PromQLSource("s", BASE, limits=Limits(max_series=1))
    with pytest.raises(LimitExceeded):
        await src.fetch_histogram("x_bucket", ["r"], RNG, 60_000)


@respx.mock
async def test_query_on_native_histograms_explains_instead_of_malformed():
    native = {
        "metric": {},
        "histograms": [[1_700_000_100, {"count": "1", "sum": "1", "buckets": []}]],
    }
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix([native])))
    src = PromQLSource("s", BASE, flavor="prometheus")
    for call in (src.fetch_values, src.fetch):
        with pytest.raises(SourceError, match="native histograms") as e:
            await call("sum(rate(lat[5m]))", RNG, 60_000)
        assert "query_distribution" in (e.value.hint or "")


@respx.mock
async def test_mixed_le_layouts_in_a_group_are_refused_with_a_hint():
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=matrix(
                [
                    {"metric": {"le": "1"}, "values": [[1_700_000_100, "1"]]},
                    {"metric": {"le": "+Inf"}, "values": [[1_700_000_100, "2"]]},
                ]
            ),
        )
    )
    instant = {"host": "vm.test", "path": "/api/v1/query"}
    respx.get(**instant).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"le": "1"}, "value": [1, "2"]},
                        {
                            "metric": {"le": "+Inf"},
                            "value": [1, "3"],
                        },  # one series has no le=1 bucket
                    ],
                },
            },
        )
    )
    with pytest.raises(SourceError, match="different bucket layouts") as e:
        await PromQLSource("s", BASE).fetch_histogram("x_bucket", [], RNG, 60_000)
    assert "group by" in (e.value.hint or "")


@respx.mock
async def test_expression_fetch_keeps_samples_without_a_value_and_drops_filled_buckets():
    # values from the subquery at 100/160 (160 lookback-filled); samples observed at 40/100;
    # series b has samples but no value anywhere (rate needs two samples)
    def responder(request):
        q = request.url.params["query"]
        if q.startswith("count_over_time(x["):
            return httpx.Response(
                200,
                json=matrix(
                    [
                        {
                            "metric": {"i": "a"},
                            "values": [[1_700_000_040, "4"], [1_700_000_100, "3"]],
                        },
                        {"metric": {"i": "b"}, "values": [[1_700_000_100, "1"]]},
                    ]
                ),
            )
        assert q.startswith(("avg_over_time((rate(x", "min_over_time((", "max_over_time(("))
        return httpx.Response(
            200,
            json=matrix(
                [{"metric": {"i": "a"}, "values": [[1_700_000_100, "1"], [1_700_000_160, "1"]]}]
            ),
        )

    respx.get(**ROUTE).mock(side_effect=responder)
    src = PromQLSource("p", BASE, flavor="prometheus")
    res = await src.fetch("rate(x[5m])", RNG, 60_000)
    sa, sb = series_id("p", {"i": "a"}), series_id("p", {"i": "b"})
    got = {(r["series_id"], r["ts_ms"]): (r["count"], r["avg"]) for r in res.buckets.to_pylist()}
    # a@100: value and samples. a@160: lookback-filled value, no samples: dropped.
    # a@40 / b@100: samples arrived but the expression has no value (rate needs two): kept with
    # their count and a null value, not read as empty (1h9.16)
    assert got == {
        (sa, 1_700_000_040_000): (4, None),
        (sa, 1_700_000_100_000): (3, 1.0),
        (sb, 1_700_000_100_000): (1, None),
    }
    assert {r["series_id"] for r in res.series.to_pylist()} == {sa, sb}
    assert res.partial == 0


_TWO_NAMES = [
    {"metric": {"__name__": n, "instance": "a"}, "values": [[1700000100, "3"]]}
    for n in ("up", "down")
]


@respx.mock
async def test_fetch_series_differing_only_by_name_are_refused_with_a_hint():
    result = [
        {**item, "metric": {**item["metric"], "rollup": "avg"}} for item in _TWO_NAMES
    ]  # MetricsQL keeps __name__ in rollup(): two names, one label set
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix(result)))
    with pytest.raises(SourceError, match="differ only by __name__") as exc:
        await PromQLSource("vm", BASE).fetch("{__name__=~'up|down'}", RNG, 60_000)
    assert "'up'" in str(exc.value) and "'down'" in str(exc.value)
    assert exc.value.hint and "label_replace" in exc.value.hint


@respx.mock
async def test_fetch_values_series_differing_only_by_name_are_refused_with_a_hint():
    respx.get(**ROUTE).mock(return_value=httpx.Response(200, json=matrix(_TWO_NAMES)))
    with pytest.raises(SourceError, match="differ only by __name__") as exc:
        await PromQLSource("vm", BASE).fetch_values("{__name__=~'up|down'}", RNG, 60_000)
    assert exc.value.hint and "sum by" in exc.value.hint


def test_histogram_matrix_series_differing_only_by_name_are_refused():
    from telemetry_nerd.analysis.histogram import from_matrix

    result = [
        {"metric": {"__name__": n, "instance": "a", "le": "1"}, "values": [[1700000100, "3"]]}
        for n in ("lat_a_bucket", "lat_b_bucket")
    ]
    with pytest.raises(SourceError, match="differ only by __name__") as exc:
        from_matrix("vm", result)
    assert exc.value.hint and "sum by" in exc.value.hint
    # different le of one name is the normal case
    ok = [
        {"metric": {"__name__": "x", "le": le}, "values": [[1700000100, "1"]]}
        for le in ("1", "+Inf")
    ]
    from_matrix("vm", ok)
