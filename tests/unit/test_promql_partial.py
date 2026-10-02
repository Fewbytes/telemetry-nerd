"""Partial responses are not complete responses (1h9.12): isPartial / warnings -> unknown spans."""

import httpx
import pytest
import respx

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.promql import PromQLSource, classify_warnings

BASE = "http://vm.test"
RNG = TimeRange(1_700_000_040_000, 1_700_000_160_000)
ROUTE = {"host": "vm.test", "path": "/api/v1/query_range"}
HIST_PROBE = {"host": "vm.test", "path": "/api/v1/query"}


def _matrix(result, **extra):
    return {"status": "success", **extra, "data": {"resultType": "matrix", "result": result}}


def _responder(**extra):
    def responder(request):
        if request.url.params["query"].startswith("rollup("):
            res = [
                {"metric": {"instance": "a", "rollup": f}, "values": [[1700000100, v]]}
                for f, v in (("min", "1"), ("max", "5"), ("avg", "3"))
            ]
        else:
            res = [{"metric": {"instance": "a"}, "values": [[1700000100, "4"]]}]
        return httpx.Response(200, json=_matrix(res, **extra))

    return responder


@respx.mock
async def test_vm_is_partial_true_marks_the_chunk_unknown_and_keeps_the_data():
    respx.get(**ROUTE).mock(side_effect=_responder(isPartial=True))
    res = await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert res.buckets.column("ts_ms").to_pylist() == [1_700_000_100_000]  # data kept
    [(a, b, reason)] = res.failed
    assert (a, b) == (RNG.start_ms, RNG.end_ms)
    assert reason.startswith("PartialResponse: ") and "isPartial" in reason


@respx.mock
async def test_prometheus_partial_warning_marks_the_chunk_unknown_with_the_source_message():
    msg = "store 10.0.0.1:10901 unavailable: partial response, some data may be missing"
    respx.get(**ROUTE).mock(side_effect=_responder(warnings=[msg]))
    res = await PromQLSource("t", BASE, flavor="prometheus").fetch("up", RNG, 60_000)
    assert res.buckets.num_rows == 1
    [(a, b, reason)] = res.failed
    assert (a, b) == (RNG.start_ms, RNG.end_ms)
    assert reason == f"PartialResponse: {msg}"


@respx.mock
async def test_informational_warning_is_a_note_not_unknown():
    info = "PromQL info: metric might not be a counter, name does not end in _total/_sum/_count"
    respx.get(**ROUTE).mock(side_effect=_responder(warnings=[info]))
    res = await PromQLSource("t", BASE, flavor="prometheus").fetch("up", RNG, 60_000)
    assert res.failed == ()
    assert res.notes == (info,)


@respx.mock
@pytest.mark.parametrize("extra", [{}, {"isPartial": False}, {"warnings": []}])
async def test_complete_responses_are_unchanged(extra):
    respx.get(**ROUTE).mock(side_effect=_responder(**extra))
    res = await PromQLSource("vm", BASE).fetch("up", RNG, 60_000)
    assert res.failed == () and res.notes == ()


@respx.mock
async def test_fetch_values_partial_is_unknown():
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=_matrix([{"metric": {}, "values": [[1_700_000_100, "1"]]}], isPartial=True),
        )
    )
    res = await PromQLSource("s", BASE).fetch_values("x / on(a) y", RNG, 60_000)
    assert res.buckets.num_rows == 1
    assert [f[2].split(":")[0] for f in res.failed] == ["PartialResponse"]


@respx.mock
async def test_fetch_histogram_partial_is_unknown():
    respx.get(**HIST_PROBE).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
        )
    )
    respx.get(**ROUTE).mock(
        return_value=httpx.Response(
            200,
            json=_matrix(
                [
                    {"metric": {"le": "0.1"}, "values": [[1_700_000_100, "9"]]},
                    {"metric": {"le": "+Inf"}, "values": [[1_700_000_100, "10"]]},
                ],
                warnings=["partial response: a store timed out"],
            ),
        )
    )
    d = await PromQLSource("s", BASE, flavor="prometheus").fetch_histogram(
        "lat_bucket", [], RNG, 60_000
    )
    assert d.columns.num_rows == 1  # data kept
    [(a, b, reason)] = d.failed
    assert (a, b) == (RNG.start_ms, RNG.end_ms)
    assert "a store timed out" in reason


@pytest.mark.parametrize(
    ("warnings", "partial", "notes"),
    [
        ([], None, []),
        (["PromQL info: x"], None, ["PromQL info: x"]),
        (
            ["PromQL warning: mix of histograms and floats"],
            None,
            ["PromQL warning: mix of histograms and floats"],
        ),
        (["store down"], "store down", []),  # unknown text: conservative, partial
        (
            ["PromQL info: x", "partial response: y", "z"],
            "partial response: y; z",
            ["PromQL info: x"],
        ),
        (["not a list entry", 3], "not a list entry; 3", []),
    ],
)
def test_warning_classification(warnings, partial, notes):
    assert classify_warnings(warnings) == (partial, notes)
