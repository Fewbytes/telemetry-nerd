"""MissingDataSemantics profiles: shape, evidence integrity, adapter wiring."""

from __future__ import annotations

import httpx
import pytest
import respx

from telemetry_nerd.model.time import TimeRange
from telemetry_nerd.sources.base import LimitExceeded
from telemetry_nerd.sources.promql import PromQLSource
from telemetry_nerd.sources.public import PUBLIC_SOURCES
from telemetry_nerd.sources.semantics import (
    PROFILES,
    Status,
    classify_limit_error,
    semantics_for,
)
from tests.unit.missing_data_fx import exists, fx


def _all_facts():
    return [
        pytest.param(profile.backend, name, fact, id=f"{profile.backend}.{name}")
        for profile in PROFILES.values()
        for name, fact in profile.facts().items()
    ]


@pytest.mark.parametrize(("backend", "name", "fact"), _all_facts())
def test_every_fact_carries_evidence_matching_its_status(backend, name, fact):
    if fact.status is Status.UNKNOWN:
        assert not fact.evidence
        return
    assert fact.evidence, f"{backend}.{name} is {fact.status} without evidence"
    if fact.status is Status.VERIFIED:
        for fid in fact.evidence:
            assert exists(fid), f"{backend}.{name}: missing fixture {fid}"
    else:
        assert all(e.startswith("https://") for e in fact.evidence)


def test_all_four_backends_have_a_profile_and_registry_backends_resolve():
    assert set(PROFILES) == {"prometheus", "thanos", "mimir", "victoriametrics"}
    for entry in PUBLIC_SOURCES.values():
        assert semantics_for(entry.backend, entry.flavor).backend == entry.backend


def test_flavor_fallback_without_a_backend():
    assert semantics_for(None, "victoriametrics").backend == "victoriametrics"
    assert semantics_for(None, "prometheus").backend == "prometheus"


def test_summary_is_short_and_names_unverified_properties():
    for profile in PROFILES.values():
        lines = profile.summary()
        assert 3 <= len(lines) <= 7
        assert (profile.unverified() == []) or any("unverified" in ln for ln in lines)


def test_vm_and_prometheus_differ_where_it_matters_for_bucket_state():
    prom, vm = PROFILES["prometheus"], PROFILES["victoriametrics"]
    assert prom.gap_fill.value.kind == "fixed" and prom.gap_fill.value.max_ms == 300_000
    assert vm.gap_fill.value.kind == "adaptive"
    assert prom.post_gap_increase_spike.value is False
    assert vm.post_gap_increase_spike.value is True
    assert PROFILES["mimir"].push_stale_markers.value is False


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        (
            (
                "exceeded maximum resolution of 11,000 points per timeseries. Try decreasing the "
                "query resolution (?step=XX)"
            ),
            "max_points",
        ),
        (
            "query processing would load too many samples into memory in query execution",
            "max_samples",
        ),
        ("too many points for the given start=1 and step=5000: 541; the maximum", "max_points"),
        ("the number of matching timeseries exceeds 3; either narrow down", "max_series"),
        ('invalid parameter "query": 1:11: parse error', None),
    ],
)
def test_classify_limit_error(message, kind):
    assert classify_limit_error(message) == kind


# --- adapter: recorded limit responses become LimitExceeded, not a generic failure ---------------

RNG = TimeRange(1_700_000_040_000, 1_700_000_160_000)


@pytest.mark.parametrize(
    "fid",
    [
        "missing-data/prometheus/prom__limit_points",
        "missing-data/prometheus/prom-limits__limit_max_samples",
        "missing-data/victoriametrics/vm-limits__limit_points",
        "missing-data/victoriametrics/vm-limits__limit_series",
        "missing-data/thanos/wikimedia-raw__step_limit_over",  # text/plain body
        "missing-data/thanos/cern-eos__step_limit_over",
        "missing-data/mimir/grafana-play__step_limit_over",
    ],
)
@respx.mock
async def test_recorded_limit_responses_raise_limit_exceeded(fid):
    rec = fx(fid)
    body = rec["body"]
    resp = (
        httpx.Response(rec["status"], text=body)
        if isinstance(body, str)
        else httpx.Response(rec["status"], json=body)
    )
    respx.get(host="src.test", path="/api/v1/query_range").mock(return_value=resp)
    src = PromQLSource("src", "http://src.test", flavor="prometheus")
    with pytest.raises(LimitExceeded) as e:
        await src.fetch("up", RNG, 60_000)
    assert e.value.hint


def test_source_exposes_its_profile():
    assert PromQLSource("vm", "http://vm.test").semantics.backend == "victoriametrics"
    prom = PromQLSource("p", "http://p.test", flavor="prometheus", backend="thanos")
    assert prom.semantics.backend == "thanos"
    assert PromQLSource("p", "http://p.test", flavor="prometheus").semantics.backend == "prometheus"
