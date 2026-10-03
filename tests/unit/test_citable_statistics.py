"""A finding resting on panels is shown the labelled op statistics behind them, and op results
say where their citable evidence is (bead hk2r). Shapes of eval round 5: overload_spike's
check_littles_law on d4 (littles_law_transient 21:16-21:17 and littles_law_backlog_growth at the
21:15 peak, special cause; the whole-range ratio, measurement system), cited only as panel p1 and
annotation a1; payment-failure's analyze(d1) (payment level shift 0.324 at 21:30, p = 2e-05,
special cause), cited only as panels."""

import json

import pytest

from telemetry_nerd.core.evidence_discipline import citable_statistics, derive_sources
from telemetry_nerd.core.littles_ops import METHOD, PROMOTION_METHOD
from telemetry_nerd.core.uncertainty import mark_statistics
from telemetry_nerd.core.wire import cite_line, with_cite
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.mcp.shapes import EvidenceContext, ShapeError, read_evidence
from telemetry_nerd.workspace.models import StatisticRef

from .test_evidence_discipline import F1, NOW, run  # noqa: F401  (the `run` fixture)

TRANSIENT_METHOD = ("relative discrepancy L / (lambda W) - 1 of a transient window, measurement "
                    "interval; source: special cause")  # fmt: skip
SHIFT_METHOD = "CUSUM changepoint (Kolmogorov null, AR(1) long-run sigma), 99% interval"


def _st(name, value, interval, method, source, dataset="d4", **params):
    return {"kind": "statistic", "dataset": dataset, "name": name, "value": value,
            "interval": interval, "exact": False, "method": method, "params": params,
            "source": source}  # fmt: skip


LITTLES = [
    _st("littles_law_ratio", 0.997, [0.955, 1.039], METHOD, "measurement_system",
        window="21:09-21:21", datasets={"L": "d4"}, unit="s"),
    _st("littles_law_backlog_growth", 908.0, [850.0, 966.0], PROMOTION_METHOD["backlog_growth"],
        "special_cause", window="2026-10-03T21:15:00Z/2026-10-03T21:16:00Z"),
    _st("littles_law_transient", 0.47, [0.31, 0.64], TRANSIENT_METHOD, "special_cause",
        window="2026-10-03T21:16:00Z/2026-10-03T21:17:00Z", phase="drain"),
    _st("mean_concurrency_L", 110.0, [105.0, 115.0], "time average of the gauge", None),
]  # fmt: skip
SHIFT = _st("level_shift", 0.324, [0.29, 0.36], SHIFT_METHOD, "special_cause", dataset="d3",
            at="2026-10-03T21:30:00+00:00", p=2e-05, n=40)  # fmt: skip
SIGMA = _st("spc_sigma", 0.004, [0.003, 0.005], "1.4826 MAD of baseline deviations",
            "common_cause", dataset="d3")  # fmt: skip


@pytest.fixture
def store(tmp_path):
    ds = DatasetStore(open_duckdb(tmp_path / "s.duckdb"), lambda k: f"{k}1", clock=lambda: 0)
    ds.record_statistics(LITTLES)
    return ds


def test_labelled_statistics_come_back_citable_special_cause_first(store):
    got = citable_statistics(store.labelled_statistics(["d4"]), [])
    assert [(s["name"], s["source"]) for s in got] == [
        ("littles_law_backlog_growth", "special_cause"),
        ("littles_law_transient", "special_cause"),
        ("littles_law_ratio", "measurement_system"),
    ]  # the level (no source) is not offered
    t = got[1]
    assert t["interval"] == [0.31, 0.64] and t["params"]["phase"] == "drain"
    assert "datasets" not in got[2]["params"]  # compact: where / when / p only
    assert len(got[0]["method"]) <= 100  # the long promotion text shortened
    # citable as is: valid evidence, and its source checks out against the op's (i6y5)
    for st in got:
        StatisticRef.model_validate(st)
    lookup = lambda st: store.statistic_sources(st["dataset"], st["name"], st["method"],
                                                st["value"])  # fmt: skip
    assert derive_sources(got, lookup)[1] == []
    # already cited: not offered again
    assert [s["name"] for s in citable_statistics(store.labelled_statistics(["d4"]), got[:2])] == [
        "littles_law_ratio"
    ]
    assert store.labelled_statistics(["d9"]) == [] and store.labelled_statistics([]) == []


def test_op_results_end_with_where_their_evidence_is():
    compact = {"summary": "Special cause in 2 window(s)", "total": {"evidence": LITTLES},
               "detail": "x"}  # fmt: skip
    out = with_cite(compact)
    assert list(out)[-1] == "cite"
    line = out["cite"]
    assert line.startswith("cite: evidence total.evidence[1] (littles_law_backlog_growth, "
                           "special_cause), total.evidence[2] (littles_law_transient")  # fmt: skip
    assert "(littles_law_ratio, measurement_system)" in line and "more)" not in line
    assert "finding_create" in line and "\n" not in line
    analyzed = {"dataset": "d3", "series": [{"labels": {"service_name": "payment"},
                "spc": {"sigma": {"evidence": SIGMA}}, "level_shifts": [{"evidence": SHIFT}]}]}  # fmt: skip
    line = cite_line(analyzed)
    assert line is not None
    assert line.startswith("cite: evidence series[0].level_shifts[0].evidence (level_shift, "
                           "special_cause), series[0].spc.sigma.evidence (spc_sigma")  # fmt: skip
    assert with_cite({"verdict": "ok"}) == {"verdict": "ok"}  # nothing to cite: no line


def test_a_bare_dataset_is_told_to_cite_the_op_statistic_first(store):
    ctx = EvidenceContext(statistics_of=lambda d: citable_statistics(
        store.labelled_statistics([d]), [], 3))  # fmt: skip
    with pytest.raises(ShapeError) as e:
        read_evidence(["d4"], ctx)
    msg = str(e.value)
    assert msg.index("littles_law_backlog_growth (special_cause)") < msg.index("cite the panel")
    assert '"name": "littles_law_backlog_growth"' in msg  # a copyable citation
    with pytest.raises(ShapeError) as e:
        read_evidence(["d5"], EvidenceContext())
    msg = str(e.value)
    assert msg.index("Cite a statistic") < msg.index("cite the panel")


async def test_finding_on_a_panel_is_shown_the_ops_labelled_statistics(run):  # noqa: F811
    """payment round 5: analyze labelled d3's level shift special cause; the finding cites the
    panel drawing d3 only, and is shown the statistic to cite (special cause first)."""
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp

    mark_statistics({"series": [{"evidence": SIGMA}, {"evidence": SHIFT}]}, run.datasets, ["d3"])
    mcp = build_mcp(run, "http://x")
    scope = {k: v for k, v in F1["scope"].items() if k != "time_range"}
    scope |= {"start": NOW - 7_200_000, "end": NOW - 3_600_000}
    claim = "payment charge errors shifted up by 0.32 at 21:30"
    async with Client(mcp) as client:
        ok = await client.call_tool("finding_create", {"claim": claim, "scope": scope,
                                                       "evidence": ["p1"]})  # fmt: skip
        out = json.loads(ok.content[0].text)
        assert out["sources"] == ["undetermined"]
        cs = out["citable_statistics"]
        assert [(s["name"], s["source"]) for s in cs] == [
            ("level_shift", "special_cause"), ("spc_sigma", "common_cause")]  # fmt: skip
        assert cs[0]["params"]["p"] == 2e-05
        assert "source is undetermined" in out["cite"] and "d3" in out["cite"]
        # cited as given: the op's special cause, nothing more to offer for it
        ok = await client.call_tool("finding_create", {"claim": claim, "scope": scope,
                                                       "evidence": ["p1", cs[0]]})  # fmt: skip
        out = json.loads(ok.content[0].text)
        assert out["sources"] == ["special_cause"] and "source_flags" not in out
        assert [s["name"] for s in out["citable_statistics"]] == ["spc_sigma"]
        assert out["cite"].startswith("the cited panels carry no variation source")
