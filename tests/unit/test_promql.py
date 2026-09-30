import httpx
import pytest
import respx

from telemetry_nerd.model.series import series_id
from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import LimitExceeded, Limits, SourceError, SourceUnavailable
from telemetry_nerd.sources.promql import PromQLSource, is_selector

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


def test_build_queries_vm_expression_uses_subquery():
    src = PromQLSource("vm", BASE, resolution_ms=15_000)
    q = src.build_queries("sum(rate(x[1m]))", 60_000)
    assert q["rollup"] == "rollup((sum(rate(x[1m])))[1m:15s])"
    assert q["count"] == "count_over_time((sum(rate(x[1m])))[1m:15s])"


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
