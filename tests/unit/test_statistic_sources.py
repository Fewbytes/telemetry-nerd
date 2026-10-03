"""A cited op statistic resolves its variation source by (dataset, name), the method only a
tie-breaker (bead tcfz), on the shapes of eval round 4's overload_spike run
(tests/fixtures/evals/overload_spike.live-sonnet-4.snapshot.json): check_littles_law emitted
littles_law_* statistics on d4 with its long METHOD text; Claude cited them with method
"check_littles_law" and no source, and the exact-method lookup lost the op's special_cause."""

import copy
import json
from pathlib import Path

import pytest

from telemetry_nerd.core.evidence_discipline import derive_sources
from telemetry_nerd.core.littles_ops import METHOD, PROMOTION_METHOD
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.evals.questions import extra_terms
from telemetry_nerd.evals.score import score
from telemetry_nerd.evals.truth import load_truth

EV = Path(__file__).parents[1] / "fixtures/evals"
RUN = "overload_spike.live-sonnet-4"
TRANSIENT_METHOD = ("relative discrepancy L / (lambda W) - 1 of a transient window, measurement "
                    "interval; source: special cause")  # fmt: skip


def _st(name, value, method, source, dataset="d4"):
    return {"kind": "statistic", "dataset": dataset, "name": name, "value": value,
            "interval": None, "method": method, "params": {}, "source": source}  # fmt: skip


# what check_littles_law (1m windows) emitted on d4 in the run, as recorded by mark_statistics:
# the whole-range ratio consistent (measurement system), the 16:30 load peak promoted
# (backlog growth, special cause), the 16:31:30 drain a transient window (special cause)
EMITTED = [
    _st("littles_law_ratio", 1.005, METHOD, "measurement_system"),
    _st("littles_law_backlog_growth", 393.0, PROMOTION_METHOD["backlog_growth"], "special_cause"),
    _st("littles_law_transient", 0.3601, TRANSIENT_METHOD, "special_cause"),
]


@pytest.fixture
def store(tmp_path):
    con = open_duckdb(tmp_path / "series.duckdb")
    ds = DatasetStore(con, lambda kind: f"{kind}1", clock=lambda: 0)
    ds.record_statistics(EMITTED)
    return ds


def _lookup(ds: DatasetStore):
    return lambda st: ds.statistic_sources(
        st["dataset"], st["name"], st.get("method") or "", st["value"]
    )


def test_a_shortened_method_still_finds_the_ops_source(store):
    assert store.statistic_sources("d4", "littles_law_backlog_growth", "check_littles_law",
                                   393) == {"special_cause"}  # fmt: skip
    assert store.statistic_sources("d4", "littles_law_transient", "", 0.3601) == {"special_cause"}
    assert store.statistic_sources("d4", "littles_law_ratio", "Littles law", 1.005) == {
        "measurement_system"
    }


def test_never_emitted_is_still_untraced(store):
    assert store.statistic_sources("d4", "mean", "check_littles_law", 1.0) is None
    assert store.statistic_sources("d5", "littles_law_ratio", METHOD, 1.005) is None


def test_the_cited_value_picks_among_windows_then_the_method_breaks_ties(store):
    store.record_statistics([_st("littles_law_transient", 0.12, TRANSIENT_METHOD, "common_cause")])
    assert store.statistic_sources("d4", "littles_law_transient", "x", 0.3601) == {"special_cause"}
    assert store.statistic_sources("d4", "littles_law_transient", "x", 0.36) == {"special_cause"}
    assert store.statistic_sources("d4", "littles_law_transient", "x", 0.12) == {"common_cause"}
    # a value no window gave: every window's source, so the ops disagree
    both = store.statistic_sources("d4", "littles_law_transient", "x", 0.5)
    assert both == {"special_cause", "common_cause"}
    # two ops, same (dataset, name, value): the method (normalised) breaks the tie
    store.record_statistics([_st("littles_law_ratio", 1.005, "another op", "undetermined")])
    assert store.statistic_sources("d4", "littles_law_ratio", "  ANOTHER   op ", 1.005) == {
        "undetermined"
    }
    assert store.statistic_sources("d4", "littles_law_ratio", "check_littles_law", 1.005) == {
        "measurement_system", "undetermined"}  # fmt: skip


def test_ops_disagreeing_is_undetermined_with_a_reason(store):
    store.record_statistics([_st("littles_law_ratio", 1.005, "another op", "special_cause")])
    cited = _st("littles_law_ratio", 1.005, "check_littles_law", None)
    _, flags = derive_sources([cited], _lookup(store))
    assert [f["flag"] for f in flags] == ["source_undetermined"]
    assert "different sources (measurement_system, special_cause)" in flags[0]["message"]


def _cited(fid: str) -> list[dict]:
    snap = json.loads((EV / f"{RUN}.snapshot.json").read_text())
    return next(f for f in snap["workspace"]["findings"] if f["id"] == fid)["evidence"]


def test_the_runs_citations_get_the_ops_sources(store):
    ev, flags = derive_sources(_cited("f2"), _lookup(store))
    assert [e.get("source") for e in ev] == [None, "special_cause", "special_cause"]
    assert [(f["evidence"], f["flag"]) for f in flags] == [
        (1, "source_derived"), (2, "source_derived")]  # fmt: skip
    ev, flags = derive_sources(_cited("f1"), _lookup(store))
    assert ev[0]["source"] == "measurement_system" and flags[0]["flag"] == "source_derived"


def test_round4_overload_rescored_with_the_derived_sources(store):
    """Offline re-score: the run's own f1/f2 citations, sources derived as the daemon now
    derives them (the recorded snapshot keeps the round-4 result: 11/12, source_label fail)."""
    snap = json.loads((EV / f"{RUN}.snapshot.json").read_text())
    fixed = copy.deepcopy(snap)
    for f in fixed["workspace"]["findings"]:
        f["evidence"], f["source_flags"] = derive_sources(f["evidence"], _lookup(store))
    t = load_truth(EV / f"{RUN}.truth.json", extra_terms("overload_spike"))
    rep = score(fixed, t)
    st = {c.id: c.status for c in rep.checks}
    assert st["source_label"] == "pass" and not [k for k, v in st.items() if v == "fail"]
    by = {f.id: f for f in rep.findings}
    assert by["f2"].sources == ["special_cause"] and by["f2"].source_ok
    assert (rep.passed, rep.applicable) == (12, 12)
