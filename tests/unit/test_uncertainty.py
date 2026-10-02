"""The evidence-statistic side of the uncertainty policy (spec §5.3) that new ops build on."""

from __future__ import annotations

from pydantic import TypeAdapter

from telemetry_nerd.core.uncertainty import EVIDENCE_FLAGS, iter_statistics, message
from telemetry_nerd.core.wire import statistic
from telemetry_nerd.exchange import fmt
from telemetry_nerd.workspace.models import EvidenceRef

ref = TypeAdapter(EvidenceRef)


def test_statistic_without_a_derivable_interval_is_unknown_not_invalid():
    for interval in (None, [None, 2.0]):
        st = statistic("d1", "residual", 0.4, interval, "Little's law, L - lambda W", {})
        assert st["uncertainty_unknown"] is True and st["interval"] is None
        assert ref.validate_python(st).uncertainty_unknown  # citable as is
    st = statistic("d1", "residual", 0.4, [0.1, 0.7], "m", {})
    assert "uncertainty_unknown" not in st and ref.validate_python(st).interval == (0.1, 0.7)


def test_iter_statistics_finds_nested_evidence():
    a, b = statistic("d1", "a", 1.0, [0, 2], "m", {}), statistic("d2", "b", 2.0, None, "m", {})
    out = {"series": [{"evidence": a}, {"x": {"evidence": b}}], "caveats": []}
    assert list(iter_statistics(out)) == [a, b]


def test_flags_and_statuses_have_words():
    assert set(EVIDENCE_FLAGS) >= {fmt.INPUT_UNCERTAINTY_UNKNOWN, fmt.UNCERTAINTY_NOT_PROPAGATED}
    for flag in (*EVIDENCE_FLAGS, fmt.NO_UNCERTAINTY):
        assert message(flag) != flag
    assert "unknown, not zero" in message(fmt.NO_UNCERTAINTY)


def test_output_status_rules():
    src = {"caveats": ["partial"], "uncertainty": None}
    interval = {"caveats": [], "uncertainty": {"method": "m", "level": 0.9}}
    exact = {"caveats": [], "uncertainty": {"exact": True}}
    unknown = {"caveats": ["no_uncertainty"]}
    status = fmt.output_uncertainty_status
    assert status([src], declared=False, propagation=None) == fmt.NO_UNCERTAINTY
    assert status([src, exact], declared=True, propagation=None) is None
    assert status([interval], declared=True, propagation=None) == fmt.UNCERTAINTY_NOT_PROPAGATED
    assert status([interval], declared=True, propagation="delta method") is None
    assert status([interval, unknown], declared=True, propagation="delta method") == (
        fmt.INPUT_UNCERTAINTY_UNKNOWN
    )
