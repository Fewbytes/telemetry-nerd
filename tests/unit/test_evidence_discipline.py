"""Evidence discipline guardrails (bead qxp), on the shapes of the real headless run on the
payment-failure demo (tests/fixtures/evals/payment-failure.live-sonnet.snapshot.json)."""

import json
from pathlib import Path

import pyarrow as pa
import pytest

from telemetry_nerd.core.claim_scope import claim_series, pinned_matchers, read_selector
from telemetry_nerd.core.evidence_discipline import (
    check_claim,
    derive_sources,
    evidence_cover,
    known_entities,
    mentions,
)
from telemetry_nerd.core.uncertainty import mark_statistics
from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult, labels_json
from telemetry_nerd.model.series import series_id as sid_of
from telemetry_nerd.workspace.models import FindingIn

from .fakes import NOW, FakeSource, make_service

FIXTURE = Path(__file__).parents[1] / "fixtures/evals/payment-failure.live-sonnet.snapshot.json"
RUN = json.loads(FIXTURE.read_text())
F1 = RUN["workspace"]["findings"][0]
H1 = RUN["workspace"]["hypotheses"][0]
EXPRS = {k: v[0] for k, v in RUN["exprs"].items() if k.startswith("d")}
SERVICES = ["payment", "checkout", "frontend", "load-generator", "flagd", "cart"]


# --- selector reading ------------------------------------------------------------------------


def test_the_real_runs_selector_is_read_with_a_note():
    sel = F1["scope"]["selector"]  # x{...} by status_code: not PromQL
    r = read_selector(sel)
    assert [str(m) for m in r.matchers] == [
        '__name__="traces_span_metrics_calls_total"',
        'service_name="payment"',
        'span_name="charge"',
    ]
    assert "needs parentheses" in r.notes[0] and "by (status_code)" in r.notes[0]


def test_pinned_labels_of_the_evidence_expression_are_checked():
    pins = pinned_matchers(EXPRS["d3"])  # sum by (status_code) (increase(x{payment,charge}))
    assert [str(p) for p in pins] == ['service_name="payment"', 'span_name="charge"']
    lb = {"e": {"status_code": "STATUS_CODE_ERROR"}, "u": {"status_code": "STATUS_CODE_UNSET"}}
    out = claim_series(F1["scope"]["selector"], lb, "traces_span_metrics_calls_total", pins)
    assert out.ids == ["e", "u"] and not out.undetermined and not out.labels_unchecked
    assert [lv for lv, _ in out.notes] == ["info"]  # only the not-PromQL note, no warning
    other = claim_series('x{service_name="checkout"}', lb, None, pins)
    assert other.mismatch_kind == "labels" and 'fixes service_name="payment"' in other.mismatch


def test_unreadable_selector_is_scope_undetermined_not_silent():
    out = claim_series('a{pod="x"} / b{pod="x"}', {"s": {"pod": "x"}})
    assert out.undetermined
    [(level, text)] = out.notes
    assert level == "warn" and text.startswith("scope undetermined")


# --- claim scope (pure) ----------------------------------------------------------------------


def _series():
    by_svc = [{"service_name": s} for s in SERVICES]
    return {
        "d1": by_svc,
        "d2": [{"service_name": s, "span_name": "x"} for s in SERVICES],
        "d3": [{"status_code": "STATUS_CODE_ERROR"}, {"status_code": "STATUS_CODE_UNSET"}],
    }


def test_the_real_runs_f1_names_services_its_evidence_does_not_cover():
    series = _series()
    known = known_entities(series, EXPRS)
    covers = {"d3": evidence_cover(EXPRS["d3"], series["d3"])}  # p1 -> d3
    out = check_claim(F1["claim"], covers, known)
    assert out.status == "beyond_evidence"
    assert out.not_covered == ['service_name="checkout"', 'service_name="frontend"']
    assert 'service_name="payment"' in out.named
    assert 'd1, d2 have service_name="checkout"' in out.hint  # where to look
    assert 'd3: service_name="payment"' in out.message
    # citing d2 as well covers them
    covers["d2"] = evidence_cover(EXPRS["d2"], series["d2"])
    assert check_claim(F1["claim"], covers, known).status == "covered"


def test_pooled_and_unreadable_evidence():
    known = known_entities(_series(), EXPRS)
    pooled = {"t": evidence_cover("sum(rate(errors_total[1m]))", [{}])}
    out = check_claim("payment errors rose", pooled, known)
    assert out.status == "beyond_evidence"  # a total over every service is not about payment
    code = {"c": evidence_cover("code:c1/out", [{}], readable=False)}
    out = check_claim("payment errors rose", code, known)
    assert out.status == "undetermined" and out.undetermined == ['service_name="payment"']
    assert check_claim("errors rose", pooled, known).status == "covered"  # names nothing


def test_mentions_are_words():
    assert mentions("PaymentService.Charge failed", "payment")
    assert mentions("the load generator", "load-generator")
    assert not mentions("payments_total", "payment")
    assert not mentions("cartography", "cart")


# --- sources of variation (pure) -------------------------------------------------------------


def _stat(**kw):
    return {"kind": "statistic", "dataset": "d1", "name": "shift", "value": 2.0,
            "interval": [1.0, 3.0], "method": "cusum", "params": {}} | kw  # fmt: skip


def test_sources_derived_from_the_op_or_flagged_undetermined():
    ev, flags = derive_sources([_stat()], lambda st: {"special_cause"})
    assert ev[0]["source"] == "special_cause" and flags[0]["flag"] == "source_derived"
    ev, flags = derive_sources([_stat()], lambda st: None)  # never emitted by an op
    assert "source" not in ev[0] and [f["flag"] for f in flags] == ["source_undetermined"]
    _, flags = derive_sources([_stat()], lambda st: {"special_cause", "common_cause"})
    assert flags[0]["flag"] == "source_undetermined" and "different sources" in flags[0]["message"]
    _, flags = derive_sources([_stat(source="undetermined")], lambda st: None)
    assert flags == []  # cited as is; undetermined is never upgraded
    # the real run's f1: a panel only, nothing attributes the variation
    _, flags = derive_sources(F1["evidence"], lambda st: None)
    assert [(f["evidence"], f["flag"]) for f in flags] == [(0, "source_undetermined")]


# --- through the service ---------------------------------------------------------------------


class SpanSource(FakeSource):
    """Span-metric shapes of the demo: by service_name, or one service by status_code."""

    async def fetch(self, expr, rng, step_ms):
        if "by (status_code)" in expr:
            labels = [{"status_code": c} for c in ("STATUS_CODE_ERROR", "STATUS_CODE_UNSET")]
        else:
            labels = [{"service_name": s} for s in SERVICES]
        ts = list(range(rng.start_ms, rng.end_ms + 1, step_ms))
        sids = [sid_of(self.name, lb) for lb in labels]
        rows = [(t, s) for s in sids for t in ts]
        n = len(rows)
        buckets = pa.table({"ts_ms": [r[0] for r in rows], "series_id": [r[1] for r in rows],
                            "avg": [1.0] * n, "min": [1.0] * n, "max": [1.0] * n,
                            "count": [max(1, step_ms // self.resolution_ms)] * n},
                           schema=BUCKET_SCHEMA)  # fmt: skip
        series = pa.table({"series_id": sids, "labels": [labels_json(lb) for lb in labels]},
                          schema=SERIES_SCHEMA)  # fmt: skip
        return FetchResult(buckets, series)


@pytest.fixture
async def run(tmp_path):
    """The real run's workspace shapes: d1 by service, d2 by service and span, d3 payment by
    status_code, p1 drawing d3."""
    svc = make_service(tmp_path, SpanSource())
    for key in ("d1", "d2", "d3"):
        assert (await svc.query(EXPRS[key], start="now-2h", end="now-1h", step="1m"))[
            "dataset"
        ] == key
    svc.show("d3", "payment charge by status")
    return svc


def f1_in(**kw) -> FindingIn:
    meta_scope = dict(F1["scope"])
    meta_scope["time_range"] = {"start_ms": NOW - 7_200_000, "end_ms": NOW - 3_600_000}
    data = {"claim": F1["claim"], "scope": meta_scope, "evidence": F1["evidence"]} | kw
    return FindingIn.model_validate(data)


async def test_f1_is_refused_with_where_the_services_are(run):
    with pytest.raises(ValueError, match="claim_beyond_evidence") as e:
        run.ws.finding_create(f1_in(), "claude")
    msg = str(e.value)
    assert 'service_name="checkout", service_name="frontend"' in msg
    assert 'd1, d2 have service_name="checkout"' in msg and "scope_note" in msg


async def test_f1_with_a_scope_note_is_flagged_beyond_evidence(run):
    f = run.ws.finding_create(f1_in(scope_note="same 79 errors read off d2 by eye"), "claude")
    assert f.scope_check is not None and f.scope_check.status == "beyond_evidence"
    assert f.scope_check.not_covered == ['service_name="checkout"', 'service_name="frontend"']
    assert f.scope_note == "same 79 errors read off d2 by eye"
    # the selector was read: no "judged over every series" fallback any more
    assert not any("every evidence series" in c for c in f.caveats)
    assert f.sources == ["undetermined"]  # a panel only: nothing attributes the variation
    assert run.ws.brief()["findings"][0]["scope"] == "beyond_evidence"


async def test_f1_citing_the_dataset_with_the_services_is_covered(run):
    errors = {"kind": "statistic", "dataset": "d2", "name": "errors", "value": 79.0, "exact": True,
              "method": "sum of increase per service"}  # fmt: skip
    ev = [*F1["evidence"], errors]
    f = run.ws.finding_create(f1_in(evidence=ev), "claude")
    assert f.scope_check is not None and f.scope_check.status == "covered"


async def test_unreadable_selector_is_stored_as_undetermined(run):
    scope = dict(F1["scope"], selector='a{service_name="payment"} / b')
    data = f1_in(claim="payment charge calls failed", scope=scope)
    data.scope.time_range.start_ms, data.scope.time_range.end_ms = NOW - 7_200_000, NOW - 3_600_000
    f = run.ws.finding_create(data, "claude")
    assert f.scope_check is not None and f.scope_check.status == "undetermined"
    assert f.scope_check.message.startswith("scope undetermined: scope.selector could not")


async def test_source_comes_back_from_the_op_that_emitted_the_statistic(run):
    st = _stat(dataset="d3", source="special_cause")
    mark_statistics({"series": [{"evidence": st}]}, run.datasets, ["d3"])
    bare = {k: v for k, v in st.items() if k != "source"}
    f = run.ws.finding_create(f1_in(claim="payment charge errors shifted", evidence=[bare]),
                              "claude")  # fmt: skip
    assert f.evidence[0].source == "special_cause"
    assert [(s.flag, s.source) for s in f.source_flags] == [("source_derived", "special_cause")]
    assert f.sources == ["special_cause"]


# --- hypotheses ------------------------------------------------------------------------------


async def test_the_real_runs_h1_cannot_be_supported(run):
    h1 = run.ws.hypothesis_create(H1["statement"], "claude")
    run.ws.finding_create(f1_in(scope_note="n", hypothesis=h1.id, stance="for"), "claude")
    with pytest.raises(ValueError, match="cannot mark h1 supported") as e:
        run.ws.hypothesis_update(h1.id, "supported", "claude")
    assert "no concrete subject" in str(e.value) and "no alternative considered" in str(e.value)
    assert run.ws.objects.get_hypothesis(h1.id).status == "proposed"


async def test_a_concrete_hypothesis_with_an_alternative_ruled_out_is_supported(run):
    h1 = run.ws.hypothesis_create("payment charge calls failing caused the order errors", "claude")
    with pytest.raises(ValueError, match="no finding for it"):
        run.ws.hypothesis_update(h1.id, "supported", "claude", alternatives_considered="x")
    run.ws.finding_create(f1_in(scope_note="n", hypothesis=h1.id, stance="for"), "claude")
    with pytest.raises(ValueError, match="no alternative considered"):
        run.ws.hypothesis_update(h1.id, "supported", "claude")
    vague = run.ws.hypothesis_create(H1["statement"], "claude")
    run.ws.hypothesis_update(vague.id, "inconclusive", "claude")
    with pytest.raises(ValueError, match="no alternative considered"):  # a vague one: no
        run.ws.hypothesis_update(h1.id, "supported", "claude")
    h2 = run.ws.hypothesis_create("checkout itself failed", "claude")
    run.ws.hypothesis_update(h2.id, "refuted", "claude")
    assert run.ws.hypothesis_update(h1.id, "supported", "claude").status == "supported"


async def test_alternatives_note_and_user_verdicts(run):
    h = run.ws.hypothesis_create("traces_span_metrics_calls_total errors on payment", "claude")
    f = run.ws.finding_create(f1_in(scope_note="n", hypothesis=h.id, stance="for"), "claude")
    out = run.ws.hypothesis_update(
        h.id, "supported", "claude", alternatives_considered="flagd: no errors in the window"
    )
    assert out.alternatives_considered == "flagd: no errors in the window"
    assert run.log.since(0)[-1].payload["alternatives_considered"].startswith("flagd")
    run.ws.finding_verdict(f.id, "rejected", "user")
    run.ws.hypothesis_update(h.id, "proposed", "claude")
    with pytest.raises(ValueError, match="the user has not rejected"):
        run.ws.hypothesis_update(h.id, "supported", "claude")
    vague = run.ws.hypothesis_create(H1["statement"], "claude")
    assert run.ws.hypothesis_update(vague.id, "supported", "user").status == "supported"


def test_stored_objects_without_the_new_fields_still_load():
    from telemetry_nerd.workspace.models import Finding, Hypothesis

    assert Hypothesis.model_validate(H1).alternatives_considered is None
    f = Finding.model_validate(F1)
    assert f.scope_check is None and f.source_flags == [] and f.scope_note is None


async def test_mcp_finding_create_shows_scope_and_sources(run):
    from mcp import Client

    from telemetry_nerd.mcp.server import build_mcp

    mcp = build_mcp(run, "http://x")
    scope = {k: v for k, v in F1["scope"].items() if k != "time_range"}
    args = {"claim": F1["claim"], "evidence": F1["evidence"],
            "scope": scope | {"start": NOW - 7_200_000, "end": NOW - 3_600_000}}  # fmt: skip
    async with Client(mcp) as client:
        refused = await client.call_tool("finding_create", args)
        assert refused.is_error and "claim_beyond_evidence" in refused.content[0].text
        ok = await client.call_tool("finding_create", args | {"scope_note": "read off d2"})
        out = json.loads(ok.content[0].text)
        assert out["scope"]["status"] == "beyond_evidence"
        assert out["sources"] == ["undetermined"]
        assert out["source_flags"][0]["flag"] == "source_undetermined"
        await client.call_tool("hypothesis_create", {"statement": "payment charge failing"})
        bad = await client.call_tool(
            "hypothesis_update", {"hypothesis": "h1", "status": "supported"}
        )
        assert bad.is_error and "no finding for it" in bad.content[0].text
