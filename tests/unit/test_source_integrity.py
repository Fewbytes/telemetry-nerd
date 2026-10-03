"""A cited statistic's variation source is checked against the op that emitted it (bead i6y5;
principle 16, spec §5.4: undetermined is never upgraded). Shapes of eval round 5's
payment-failure run (tests/fixtures/evals/payment-failure.live-sonnet-5.*): analyze(d1) gave
payment `level_shifted`, a CUSUM level shift of 0.324 at 21:30 with p = 2e-05, special cause;
a departure from zero the cautious (clustered) model could not decide was undetermined."""

import json
from pathlib import Path

import pytest

from telemetry_nerd.core.evidence_discipline import derive_sources
from telemetry_nerd.datasets.db import open_duckdb
from telemetry_nerd.datasets.store import DatasetStore
from telemetry_nerd.evals.questions import extra_terms
from telemetry_nerd.evals.score import op_undetermined, score_finding
from telemetry_nerd.evals.truth import load_truth

EV = Path(__file__).parents[1] / "fixtures/evals"
RUN = "payment-failure.live-sonnet-5"

SHIFT_METHOD = "CUSUM changepoint (Kolmogorov null, AR(1) long-run sigma), 99% interval"
DEPARTURE_METHOD = "departure from zero (Poisson and clustered models)"


def _st(name, value, interval, method, source=None, dataset="d1", **params):
    out = {"kind": "statistic", "dataset": dataset, "name": name, "value": value,
           "interval": interval, "method": method, "params": params}  # fmt: skip
    return out | ({"source": source} if source is not None else {})


SHIFT = _st("level_shift", 0.324, [0.29, 0.36], SHIFT_METHOD, "special_cause",
            at="2026-10-03T21:30:00+00:00", p=2e-05)  # fmt: skip
DEPARTURE = _st("departure_from_zero", 0.05, [0.01, 0.09], DEPARTURE_METHOD, "undetermined",
                p_poisson=2e-24, p_clustered=0.015)  # fmt: skip
SIGMA = _st("spc_sigma", 0.004, [0.003, 0.005], "1.4826 MAD of baseline deviations",
            "common_cause")  # fmt: skip
LEVEL = _st("mean", 0.31, [0.3, 0.32], "mean over the window")  # an op's level: no source


@pytest.fixture
def lookup(tmp_path):
    ds = DatasetStore(open_duckdb(tmp_path / "s.duckdb"), lambda k: f"{k}1", clock=lambda: 0)
    ds.record_statistics([SHIFT, DEPARTURE, SIGMA, LEVEL])
    return lambda st: ds.statistic_sources(
        st["dataset"], st["name"], st.get("method") or "", st["value"]
    )


def _as(st, source):
    return {k: v for k, v in st.items() if k != "source"} | (
        {"source": source} if source is not None else {}
    )


def test_an_upgrade_of_the_ops_undetermined_is_refused(lookup):
    with pytest.raises(ValueError, match="source_relabelled") as e:
        derive_sources([_as(DEPARTURE, "special_cause")], lookup)
    msg = str(e.value)
    assert "cited source special_cause" in msg and "labelled it undetermined" in msg
    assert "departure from zero" in msg  # the op's model, so the caller sees why
    assert "without source" in msg and "source undetermined" in msg  # the two ways out


def test_swapping_a_label_is_refused_too(lookup):
    with pytest.raises(ValueError, match="labelled it common_cause"):
        derive_sources([_as(SIGMA, "special_cause")], lookup)
    with pytest.raises(ValueError, match="labelled it special_cause"):
        derive_sources([_as(SHIFT, "common_cause")], lookup)


def test_the_ops_own_label_is_accepted_as_cited(lookup):
    ev, flags = derive_sources([SHIFT, DEPARTURE], lookup)
    assert [e["source"] for e in ev] == ["special_cause", "undetermined"] and flags == []
    # cited at the precision shown, with a shortened method: still the op's statistic
    ev, flags = derive_sources([_as(SHIFT, "special_cause") | {"value": 0.32,
                                                               "method": "analyze"}], lookup)  # fmt: skip
    assert flags == []


def test_a_downgrade_to_undetermined_is_kept_and_flagged(lookup):
    """Policy: undetermined can only say less than the op's model (principle 16: a label must
    hold under the cautious model, else undetermined), so it is accepted; the op's label stays
    in the flag, and nothing scores it as the op's own undetermined."""
    ev, flags = derive_sources([_as(SHIFT, "undetermined")], lookup)
    assert ev[0]["source"] == "undetermined"
    assert [(f["flag"], f["source"]) for f in flags] == [("source_downgraded", "undetermined")]
    assert "labelled it special_cause" in flags[0]["message"]
    assert not op_undetermined({"evidence": ev, "source_flags": flags})
    assert op_undetermined({"evidence": [DEPARTURE], "source_flags": []})


def test_a_label_no_op_stands_behind_is_flagged_unverified(lookup):
    untraced = _st("error_share", 0.95, [0.9, 0.99], "by eye", "special_cause")
    ev, flags = derive_sources([untraced], lookup)
    assert ev[0]["source"] == "special_cause"
    assert [(f["flag"], f["source"]) for f in flags] == [("source_unverified", "special_cause")]
    # an op that attributes no variation to a statistic (a level) backs no label either
    _, flags = derive_sources([_as(LEVEL, "special_cause")], lookup)
    assert [f["flag"] for f in flags] == ["source_unverified"]
    # undetermined says nothing it could not back
    assert derive_sources([_as(untraced, "undetermined")], lookup)[1] == []
    assert derive_sources([_as(LEVEL, "undetermined")], lookup)[1] == []


def test_ops_disagreeing_leave_undetermined_the_only_label(tmp_path):
    ds = DatasetStore(open_duckdb(tmp_path / "s.duckdb"), lambda k: f"{k}1", clock=lambda: 0)
    ds.record_statistics([SHIFT, _as(SHIFT, "common_cause") | {"method": "another op"}])

    def lookup(st):
        return ds.statistic_sources(st["dataset"], st["name"], st["method"], st["value"])

    with pytest.raises(ValueError, match="different sources"):
        derive_sources([_as(SHIFT, "special_cause") | {"method": "x"}], lookup)
    _, flags = derive_sources([_as(SHIFT, "undetermined") | {"method": "x"}], lookup)
    assert flags == []
    # the method names the op: its label is the one checked
    assert derive_sources([SHIFT], lookup)[1] == []


def test_the_scorer_does_not_trust_an_unverified_label():
    """Round 5's payment f2 (about payment, citing p3 = d4), re-cited with a special_cause label
    typed on a statistic no op emitted: counted as undetermined, not special cause."""
    snap = json.loads((EV / f"{RUN}.snapshot.json").read_text())
    truth = load_truth(EV / f"{RUN}.truth.json", extra_terms("payment-failure"))
    ws = snap["workspace"]
    f2 = next(f for f in ws["findings"] if f["id"] == "f2")
    typed = _st("error_share", 0.95, [0.9, 0.99], "by eye", "special_cause", dataset="d4")
    panels = {p["id"] for p in ws["panels"]}

    def scored(flags):
        return score_finding(f2 | {"evidence": [typed], "source_flags": flags}, truth,
                             snap["exprs"], panels)  # fmt: skip

    trusted = scored([])
    unverified = scored([{"evidence": 0, "flag": "source_unverified", "source": "special_cause",
                          "message": "m"}])  # fmt: skip
    assert trusted.sources == ["special_cause"] and trusted.source_ok
    assert unverified.sources == ["undetermined"] and unverified.source_ok is False
