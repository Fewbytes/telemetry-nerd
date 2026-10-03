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


# --- q1p: eval round 3 (overload_spike.live-sonnet-3) ------------------------------------------
# entities(kind=service) found one service (checkout) over 13:43-14:43Z; d6 = sum(rate(
# http_requests_total[1m])) had no same-metric witness, d7 is a ratio of two sums, and f1's
# scope.selector listed the three check_littles_law metrics.

RUN3 = json.loads(
    (Path(__file__).parents[1] / "fixtures/evals/overload_spike.live-sonnet-3.snapshot.json")
    .read_text()
)  # fmt: skip
EXPRS3 = {k: v[0] for k, v in RUN3["exprs"].items() if k.startswith("d")}
F3_CLAIM = ("checkout request rate (likely completion-side) rose from a ~15.0/s median baseline "
            "to a peak of 44.7/s at 14:39:00 (~3x)")  # fmt: skip
F2_CLAIM = "checkout mean latency level-shifted up by 15.5 s (99% CI 10.2-20.7) at 14:38:45"
F1_SELECTOR = "http_server_active_requests, http_requests_total, http_request_duration_seconds"
WINDOW = (0, 1000)  # the entities window holds the datasets' range (100..200)


def listing(values=("checkout",), **kw):
    from telemetry_nerd.core.evidence_discipline import Listing

    args = {"source": "default", "label": "service", "values": tuple(values), "truncated": False,
            "start_ms": WINDOW[0], "end_ms": WINDOW[1], "via": "entities(service)"} | kw  # fmt: skip
    return Listing(**args)


def cover3(did, listings=()):
    def single(leaf):
        return single_values(ds(did, [], 100, 200, expr=leaf), [], listings)

    return evidence_cover(EXPRS3[did], [{}], True, single)


KNOWN3 = {"service": {"checkout": {"d5"}}, "pod": {"checkout-0": {"d5"}}}


def test_f3_pooled_rate_of_a_one_service_source_is_covered_by_the_entities_listing():
    covers = {"d6": cover3("d6", [listing()])}
    scope = check_claim(F3_CLAIM, covers, KNOWN3)
    assert scope.status == "covered" and scope.named == ['service="checkout"']
    assert "single-value witness" in scope.message and "entities(service)" in scope.message


@pytest.mark.parametrize(
    ("li", "why"),
    [
        (listing(("checkout", "cart")), "two values"),
        (listing(truncated=True), "truncated"),
        (listing(source="other"), "another source"),
        (listing(start_ms=150), "window does not hold the range"),
        (listing(metric="http_server_active_requests"), "listed for another metric"),
        (listing(()), "no value"),
    ],
)
def test_no_listing_witness_without_a_sound_listing(li, why):
    scope = check_claim(F3_CLAIM, {"d6": cover3("d6", [li])}, KNOWN3)
    assert scope.status == "beyond_evidence", why


def test_a_listing_of_the_same_metric_counts_histogram_members_as_the_base():
    li = listing(metric="http_request_duration_seconds")
    covers = {"d7": cover3("d7", [li])}
    assert check_claim(F2_CLAIM, covers, KNOWN3).status == "covered"


def test_f3_hint_does_not_offer_another_metrics_dataset():
    from telemetry_nerd.core.evidence_discipline import expr_metrics

    metrics_of = {d: expr_metrics(e) for d, e in EXPRS3.items()}
    scope = check_claim(F3_CLAIM, {"d6": cover3("d6")}, KNOWN3, metrics_of=metrics_of)
    assert scope.status == "beyond_evidence"
    assert 'd5 has service="checkout"' not in scope.hint  # d5 is concurrency, not the rate
    assert "no dataset of http_requests_total" in scope.hint and "entities" in scope.hint
    # without metrics the old hint stands (callers that know no metrics)
    assert 'd5 has service="checkout"' in check_claim(F3_CLAIM, {"d6": cover3("d6")}, KNOWN3).hint


def test_f2_ratio_of_two_sums_is_read_not_undetermined():
    pooled = check_claim(F2_CLAIM, {"d7": cover3("d7")}, KNOWN3)
    assert pooled.status == "beyond_evidence" and pooled.undetermined == []  # read: pooled
    assert "each side" not in pooled.message  # both sides pooled alike: said once
    assert check_claim(F2_CLAIM, {"d7": cover3("d7", [listing()])}, KNOWN3).status == "covered"


@pytest.mark.parametrize(
    ("expr", "checkout", "cart"),
    [
        ('sum(rate(x{service="checkout"}[1m])) / sum(rate(y{service="checkout"}[1m]))', True,
         False),
        ('sum(rate(x{service="checkout"}[1m])) / sum(rate(y[1m]))', False, False),  # mixed
        ('x{service="checkout"} or x{service="cart"}', True, True),
        ('x{service="checkout"} and x', True, False),
        ('x{service="checkout"} unless y{service="cart"}', True, False),
        ('x{service="checkout"} / on(pod) y{service="checkout"}', None, None),  # not matched
        ('label_replace(x{service="checkout"}, "a", "$1", "b", "(.*)")', None, None),
        ('x{service="checkout"} * 100', True, False),
    ],
)  # fmt: skip
def test_binary_operators_intersect_or_union_coverage(expr, checkout, cart):
    c = evidence_cover(expr, [{}], True)["service"]
    assert (c.covers("checkout"), c.covers("cart")) == (checkout, cart), c


def test_f1_several_selectors_are_read_as_any_of_them():
    from telemetry_nerd.core.claim_scope import claim_series, read_expr

    read = read_expr(F1_SELECTOR)
    assert read.alternatives is not None and len(read.alternatives) == 3
    assert "several selectors" in read.notes[0]
    # each evidence dataset is checked against the selector naming its metric
    lb = {"a": {"pod": "checkout-0"}}
    assert claim_series(F1_SELECTOR, lb, metric="http_requests_total").ids == ["a"]
    assert claim_series(F1_SELECTOR, lb, metric="up").mismatch_kind == "metric"


class OneServiceIndex(OneServiceSource):
    """OneServiceSource with a label index, as entities reads it."""

    async def label_values(self, label, match=(), rng=None, limit=None):
        if label == "service":
            return list(self.services)
        if label == "__name__":
            return ["http_requests_total"]
        return []


def _f3(selector="sum(rate(http_requests_total[1m]))") -> FindingIn:
    return FindingIn.model_validate({
        "claim": F3_CLAIM,
        "scope": {"source": "default", "selector": selector,
                  "time_range": {"start_ms": NOW - 7_200_000, "end_ms": NOW - 3_600_000},
                  "step": "1m", "aggregation": "sum"},
        "evidence": [{"kind": "statistic", "dataset": "d2", "name": "peak", "value": 3.0,
                      "method": "max", "uncertainty_unknown": True}],
    })  # fmt: skip


@pytest.mark.parametrize(
    ("services", "status"), [(("checkout",), "covered"), (("checkout", "cart"), None)]
)
async def test_service_reads_the_entities_listing_as_witness(tmp_path, services, status):
    svc = make_service(tmp_path, OneServiceIndex(services))
    # d1: another metric naming checkout (as d5 did in the run); d2: the pooled rate cited
    await svc.query("http_server_active_requests", start="now-2h", end="now-1h", step="1m")
    await svc.query("sum(rate(http_requests_total[1m]))", start="now-2h", end="now-1h", step="1m")
    with pytest.raises(ValueError, match="claim_beyond_evidence"):  # no witness yet
        svc.ws.finding_create(_f3(), "claude")
    await svc.entity_index.entities(source="default", kind="service", window="3h", end="now")
    if status is None:
        with pytest.raises(ValueError, match="claim_beyond_evidence"):
            svc.ws.finding_create(_f3(), "claude")
        return
    f = svc.ws.finding_create(_f3(), "claude")
    assert f.scope_check is not None and f.scope_check.status == "covered"
    assert "entities(service: one value over" in f.scope_check.message
