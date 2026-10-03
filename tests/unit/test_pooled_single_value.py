"""Pooled evidence over a single label value covers that value (1w7), on the shapes of the
overload_spike live run (tests/fixtures/evals/overload_spike.live-sonnet.snapshot.json):
check_littles_law pooled `sum (http_server_active_requests)` (d5) while the source has one
service, which d1 (`http_server_active_requests`, unaggregated) shows."""

import json
from pathlib import Path

import pyarrow as pa
import pytest

from telemetry_nerd.core.evidence_discipline import (
    Fetched,
    check_claim,
    evidence_cover,
    known_entities,
    single_values,
)
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.series import series_id as sid_of
from telemetry_nerd.workspace.models import FindingIn

from .fakes import NOW, FakeSource, make_service

RUN = json.loads(
    (Path(__file__).parents[1] / "fixtures/evals/overload_spike.live-sonnet.snapshot.json")
    .read_text()
)  # fmt: skip
EXPRS = {k: v[0] for k, v in RUN["exprs"].items() if k.startswith("d")}
F1 = RUN["workspace"]["findings"][0]
CHECKOUT = [{"service": "checkout", "pod": "checkout-0"}, {"service": "checkout", "pod": "c-1"}]


def ds(did, series, start=0, end=100, source="default", expr=None):
    return Fetched(did, expr or EXPRS[did], source, start, end, series)


def test_the_real_runs_d5_covers_checkout_through_d1():
    d5, d1 = ds("d5", [{}]), ds("d1", CHECKOUT)
    single = single_values(d5, [d1, ds("d6", [{}]), ds("d7", [{}])])
    assert single == {"service": ("checkout", ["d1"])}
    covers = {"d5": evidence_cover(EXPRS["d5"], [{}], True, single)}
    known = known_entities({"d1": CHECKOUT}, EXPRS)
    scope = check_claim(F1["claim"], covers, known)
    assert scope.status == "covered" and scope.named == ['service="checkout"']
    assert "pooled over its only service value, per d1" in covers["d5"]["service"].describe(
        "service"
    )


@pytest.mark.parametrize(
    ("witness", "why"),
    [
        (ds("d1", [*CHECKOUT, {"service": "cart"}]), "two values"),
        (ds("d1", CHECKOUT, start=50), "covers less time"),
        (ds("d1", CHECKOUT, source="other"), "another source"),
        (ds("d1", CHECKOUT, expr='http_server_active_requests{pod="checkout-0"}'), "narrower"),
        (ds("d1", CHECKOUT, expr="http_requests_total"), "another metric"),
        (ds("d1", [{"pod": "checkout-0"}]), "label aggregated away"),
        (ds("d1", [*CHECKOUT, {"pod": "x"}]), "label missing on a series"),
        (ds("d1", CHECKOUT, expr="a / http_server_active_requests"), "unreadable"),
    ],
)
def test_no_single_value_without_a_sound_witness(witness, why):
    assert "service" not in single_values(ds("d5", [{}]), [witness]), why
    covers = {"d5": evidence_cover(EXPRS["d5"], [{}], True, None)}
    scope = check_claim(F1["claim"], covers, known_entities({"d6": [{}]}, EXPRS))
    assert scope.status == "beyond_evidence"  # still pooled: d6 names checkout by matcher


def test_query_fixing_the_label_to_one_value_covers_it():
    # sum(x{service="checkout"}): the matcher already pins it (no witness needed)
    covers = {"d6": evidence_cover(EXPRS["d6"], [{}], True)}
    assert covers["d6"]["service"].covers("checkout") is True
    assert covers["d6"]["service"].covers("cart") is False


def test_a_witness_with_the_same_restrictions_counts():
    pooled = ds("dx", [{}], expr='sum(rate(http_requests_total{code="500"}[1m]))')
    w = ds("dy", CHECKOUT, expr='sum by (service) (rate(http_requests_total{code="500"}[1m]))')
    assert single_values(pooled, [w]) == {"service": ("checkout", ["dy"])}


class OneServiceSource(FakeSource):
    """One service, two pods; `sum(...)` without `by` pools them into one unlabelled series."""

    def __init__(self, services=("checkout",)):
        super().__init__()
        self.services = services

    async def fetch(self, expr, rng, step_ms):
        if expr.startswith("sum"):
            labels = [{}]
        else:
            labels = [{"service": s, "pod": f"{s}-{k}"} for s in self.services for k in (0, 1)]
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        sids = [sid_of(self.name, lb) for lb in labels]
        rows = [(t, s) for s in sids for t in ts]
        n = len(rows)
        buckets = pa.table({"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
                            "avg": [3.0] * n, "min": [3.0] * n, "max": [3.0] * n,
                            "count": [1] * n}, schema=BUCKET_SCHEMA)  # fmt: skip
        series = pa.table({"series_id": sids, "labels": [labels_json(lb) for lb in labels]},
                          schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)


def _finding(claim: str) -> FindingIn:
    return FindingIn.model_validate({
        "claim": claim,
        "scope": {"source": "default", "selector": "sum (http_server_active_requests)",
                  "time_range": {"start_ms": NOW - 7_200_000, "end_ms": NOW - 3_600_000},
                  "step": "1m", "aggregation": "sum"},
        "evidence": [{"kind": "statistic", "dataset": "d2", "name": "mean", "value": 3.0,
                      "method": "mean of buckets", "interval": [2.9, 3.1]}],
    })  # fmt: skip


@pytest.mark.parametrize(
    ("services", "status"), [(("checkout",), "covered"), (("checkout", "cart"), None)]
)
async def test_service_reads_the_witness(tmp_path, services, status):
    svc = make_service(tmp_path, OneServiceSource(services))
    for expr in ("http_server_active_requests", "sum (http_server_active_requests)"):
        await svc.query(expr, start="now-2h", end="now-1h", step="1m")
    claim = "checkout concurrency averaged 3 in-flight requests"
    if status is None:  # two services: pooling is not about checkout alone
        with pytest.raises(ValueError, match="claim_beyond_evidence"):
            svc.ws.finding_create(_finding(claim), "claude")
        return
    f = svc.ws.finding_create(_finding(claim), "claude")
    assert f.scope_check is not None and f.scope_check.status == status
