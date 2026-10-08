"""ElasticsearchMissingDataSemantics: shape, evidence integrity, adapter wiring (bead
telemetry-nerd-sgb.6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from telemetry_nerd.sources.elasticsearch import ElasticsearchSource
from telemetry_nerd.sources.semantics import ELASTICSEARCH, Status

REPO_ROOT = Path(__file__).resolve().parents[2]


def _test_exists(evidence: str) -> bool:
    """`evidence` is "tests/.../file.py::test_name"; the test actually exists in that file."""
    path, _, name = evidence.partition("::")
    full = REPO_ROOT / path
    return full.is_file() and f"def {name}(" in full.read_text()


@pytest.mark.parametrize(
    ("name", "fact"),
    list(ELASTICSEARCH.facts().items()),
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_every_fact_carries_evidence_matching_its_status(name, fact):
    if fact.status is Status.UNKNOWN:
        assert not fact.evidence
        return
    assert fact.evidence, f"{name} is {fact.status} without evidence"
    if fact.status is Status.VERIFIED:
        for e in fact.evidence:
            assert _test_exists(e), f"{name}: no such test {e}"
    else:
        assert all(e.startswith("https://") for e in fact.evidence)


def test_source_exposes_the_profile():
    src = ElasticsearchSource(
        "es", "http://es.test:9200", index_pattern="*", time_field="@timestamp"
    )
    assert src.semantics is ELASTICSEARCH
    assert src.semantics.backend == "elasticsearch"


def test_partial_response_signal_is_not_overclaimed_as_verified():
    """A real shard failure / query timeout was not reproduced against a live cluster (a healthy
    single-node test cluster cannot be made to fail on demand); the field shapes are documented
    from the Elasticsearch search API, not verified by reproduction."""
    assert ELASTICSEARCH.partial_response_signal.status is Status.DOCUMENTED


def test_unverified_lists_only_the_honestly_unproven_facts():
    assert ELASTICSEARCH.unverified() == ["partial_response_signal"]
