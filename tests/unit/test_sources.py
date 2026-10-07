"""Variation-source vocabulary (spec §5.4, bead gkk): shared constants, wire field, findings."""

import pytest
from pydantic import ValidationError

from telemetry_nerd.analysis import sources
from telemetry_nerd.analysis.littles import COMMON, MEASUREMENT, SPECIAL
from telemetry_nerd.core.wire import statistic
from telemetry_nerd.workspace.models import FindingIn, StatisticRef

SCOPE = {
    "source": "default", "selector": "up", "time_range": {"start_ms": 0, "end_ms": 60_000},
    "step": "15s", "aggregation": "none",
}  # fmt: skip


def test_littles_reuses_the_shared_vocabulary():
    assert (COMMON, SPECIAL, MEASUREMENT) == (
        sources.COMMON, sources.SPECIAL, sources.MEASUREMENT,
    )  # fmt: skip
    assert set(sources.SOURCES) == {
        "common_cause", "special_cause", "measurement_system", "undetermined",
    }  # fmt: skip
    assert sources.text(None) == "source undetermined"


def test_statistic_source_is_optional_and_checked():
    plain = statistic("d1", "x", 1.0, [0.5, 1.5], "m", {})
    assert "source" not in plain  # backward compatible: absent unless an op labels it
    st = statistic("d1", "x", 1.0, [0.5, 1.5], "m", {}, source="special_cause")
    assert st["source"] == "special_cause"
    assert StatisticRef.model_validate(st).source == "special_cause"
    unknown = statistic("d1", "x", 1.0, None, "m", {}, source="undetermined")
    assert unknown["uncertainty_unknown"] and unknown["source"] == "undetermined"
    with pytest.raises(ValueError, match="variation source"):
        statistic("d1", "x", 1.0, [0, 2], "m", {}, source="noise")
    with pytest.raises(ValidationError):
        StatisticRef.model_validate({**st, "source": "noise"})


def test_finding_lists_the_sources_it_cites():
    a = statistic("d1", "a", 1.0, [0.5, 1.5], "m", {}, source="common_cause")
    b = statistic("d1", "b", 2.0, [1.5, 2.5], "m", {})
    c = statistic("d1", "c", 3.0, [2.5, 3.5], "m", {}, source="special_cause")
    f = FindingIn.model_validate({"claim": "x", "scope": SCOPE, "evidence": [a, b, c, a]})
    assert f.sources == ["common_cause", "special_cause"]
    old = FindingIn.model_validate({"claim": "x", "scope": SCOPE, "evidence": [b]})
    assert old.sources == []


def test_measurement_items_come_from_caveats():
    items = sources.measurement_items(["heavy_tails", "gaps", "partial", "gaps"])
    assert [i["caveat"] for i in items] == ["gaps", "partial"]
    assert {i["source"] for i in items} == {"measurement_system"}
    with pytest.raises(ValueError):
        sources.item("noise", "x")


from telemetry_nerd.model.discovery import Discovery
from telemetry_nerd.sources.base import language_of
from telemetry_nerd.sources.promql import PromQLSource


def test_promql_sources_speak_promql_and_sources_without_the_attribute_default_to_it():
    assert PromQLSource("p", "http://vm.test").query_language == "promql"
    assert language_of(PromQLSource("p", "http://vm.test")) == "promql"
    assert language_of(object()) == "promql"


def test_discovery_naming_defaults_to_prometheus_conventions():
    assert Discovery((), (), {}, None, 1.0, (), False).naming == "prometheus"
