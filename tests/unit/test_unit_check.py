"""telemetry-nerd-lei: an asserted show() unit is checked, never silently trusted."""

import pytest

from telemetry_nerd.charts.unit_check import check_unit
from telemetry_nerd.model.discovery import Discovery, MetricInfo

from .fakes import FakeSource, make_service


@pytest.mark.parametrize(
    ("provided", "expected", "factor"),
    [
        ("%", "ratio", "100"),  # a 0-1 fraction labelled percent
        ("%", "s/s", "100"),  # rate of a seconds counter: a fraction of time
        ("ratio", "%", "1/100"),
        ("ms", "s", "1000"),
        ("s", "ms", "1/1000"),
        ("bits", "B", "8"),
        ("b/s", "B/s", "8"),
        ("MiB", "B", "1/1.04858e+06"),
    ],
)
def test_a_scale_conflict_is_refused_with_the_factor(provided, expected, factor):
    c = check_unit(provided, expected, "rule x", None)
    assert c.problem and f"multiplied by {factor};" in c.problem and "catalog_write" in c.problem


def test_percent_over_a_fraction_says_how_to_make_it_true():
    c = check_unit("%", "ratio", "rule: 1 - X", (0.1, 0.9))
    assert "100 * (...)" in c.problem and "unit='ratio'" in c.problem


def test_a_different_dimension_is_refused():
    c = check_unit("B", "s", "declared UNIT", None)
    assert c.problem and "one of the two is wrong" in c.problem


@pytest.mark.parametrize(
    ("provided", "expected"),
    [("ms", "ms"), ("ratio", "s/s"), ("percent", "%"), ("req/s", "count/s"), ("bytes", "B")],
)
def test_an_agreeing_unit_is_marked_verified(provided, expected):
    c = check_unit(provided, expected, "name rule", None)
    assert c.problem is None and c.warning is None
    assert c.provenance_note == f"agrees with {expected} (name rule)"


def test_no_expectation_checks_percent_against_the_data():
    c = check_unit("%", None, None, (0.02, 0.97))
    assert c.problem is None and "unit='ratio'" in c.warning and c.provenance_note is None
    assert check_unit("%", None, None, (2.0, 97.0)).warning is None
    assert "unit='%'" in check_unit("ratio", None, None, (0.0, 42.0)).warning
    assert check_unit("ratio", None, None, (0.0, 0.5)).warning is None


def test_an_uninterpretable_unit_is_kept_but_flagged():
    c = check_unit("widgets", "B", "declared UNIT", None)
    assert c.problem is None and "could not be checked" in c.warning
    assert c.provenance_note.startswith("unverified: the catalog says B")
    assert check_unit("widgets", None, None, None) == check_unit("items", None, None, None)


DISC = Discovery(
    (MetricInfo("node_cpu_seconds_total", "counter"), MetricInfo("tn_demo_latency_seconds")),
    (), {}, None, 1.0, (), False,
)  # fmt: skip


@pytest.fixture
async def svc(tmp_path):
    svc = make_service(tmp_path, FakeSource(name="default", discovery=DISC))
    await svc.learn("default")
    return svc


async def test_show_refuses_percent_on_one_minus_idle_rate(svc):
    """The p23 bug: 1 - rate(idle) is a 0-1 fraction; unit='%' implies 0-100."""
    expr = '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))'
    ds = (await svc.query(expr, start="now-2h", end="now-1h"))["dataset"]
    with pytest.raises(ValueError, match=r"100 \* \(\.\.\.\)"):
        svc.show(ds, "How busy?", unit="%")
    ok = svc.show(ds, "How busy?", unit="ratio")
    assert "agrees with ratio" in ok.panel.spec["y"]["unit_provenance"]


async def test_show_keeps_an_unverifiable_unit_with_a_warning(svc):
    ds = (await svc.query("tn_demo_latency_seconds", start="now-2h", end="now-1h"))["dataset"]
    res = svc.show(ds, "How slow?", unit="widgets")
    assert res.panel.spec["y"]["unit"] == "widgets"
    assert "unverified" in res.panel.spec["y"]["unit_provenance"]
    assert any(i.rule == "unit_unverified" for i in res.issues)


async def test_a_quotient_is_not_checked_against_its_operands_unit(svc):
    expr = "tn_demo_latency_seconds / tn_demo_latency_seconds"
    ds = (await svc.query(expr, start="now-2h", end="now-1h"))["dataset"]
    assert svc.show(ds, "share?", unit="ratio").panel.spec["y"]["unit"] == "ratio"
