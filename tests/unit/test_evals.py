"""Scenario eval scoring (bead d77.3) on canned snapshots: no Claude, no demo, no daemon."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

from telemetry_nerd.evals import live
from telemetry_nerd.evals.questions import DEMO, extra_terms, question_for
from telemetry_nerd.evals.report import markdown
from telemetry_nerd.evals.score import (
    coverage,
    mentions,
    score,
    transcript_unscoped,
)
from telemetry_nerd.evals.truth import MEASUREMENT, SPECIAL, load_truth

FX = Path(__file__).parents[1] / "fixtures"
SCEN = FX / "scenarios"
EV = FX / "evals"


def truth(name: str):
    p = SCEN / f"{name}.json"
    if not p.exists():
        p = EV / f"{name}.truth.json"
    return load_truth(p, extra_terms(name))


def snap(name: str) -> dict:
    return json.loads((EV / f"{name}.snapshot.json").read_text())


def finding(s: dict, fid: str) -> dict:
    return next(f for f in s["workspace"]["findings"] if f["id"] == fid)


def status(rep, cid: str) -> str:
    return rep.check(cid).status


# --- ground truth


@pytest.mark.parametrize("path", sorted(SCEN.glob("*.json")), ids=lambda p: p.stem)
def test_every_demo_fixture_loads(path: Path):
    t = load_truth(path, extra_terms(path.stem))
    assert t.kind == "demo" and t.entity_label == "service_name"
    assert t.fault_start_ms < t.fault_end_ms and t.run_start_ms <= t.fault_start_ms
    assert t.root_cause_terms[0] == t.raw["root_cause"]["service"]
    assert not set(t.control) & t.implicated
    assert t.expected_sources == (SPECIAL,)


def test_homepage_flood_root_cause_is_the_load_generator_not_the_frontend():
    t = truth("homepage-flood")
    assert "frontend" in t.origin and "frontend" not in t.root_cause_terms
    assert "load-generator" in t.root_cause_terms and "traffic surge" in t.root_cause_terms


def test_signal_measures_and_directions():
    t = truth("catalog-lock-contention")
    assert t.expected_direction("frontend", "latency") == "up"
    assert t.expected_direction("frontend", "rate") == "down"
    assert t.expected_direction("frontend") is None  # mixed: no single direction
    assert truth("payment-failure").expected_direction("ad") == "flat"  # a control


def test_queue_sim_truth():
    t = truth("overload_spike")
    assert t.kind == "queue_sim" and t.entity_label == "pod"
    # a fault on every pod faults the service: claims naming "checkout" are about the origin
    assert t.origin == ("checkout-0", "checkout-1", "checkout-2", "checkout") and t.control == ()
    assert t.sole == "checkout"
    assert t.expected_sources == (SPECIAL,)
    # the effect lasts until the backlog drains (t=420s), not until the load stops (360s)
    assert t.fault_end_ms == t.run_start_ms + 420_000
    assert not t.no_onset


def test_queue_sim_whole_run_fault_has_no_onset_and_names_the_instance():
    gt = {
        "scenario": "missing_instance", "service": "checkout", "group_by": "pod",
        "instances": ["checkout-0", "checkout-1", "checkout-2"], "duration_s": 600,
        "scrape_interval_s": 5, "started_unix_ms": 1_000_000, "ended_unix_ms": 1_600_000,
        "faults": [{"kind": "missing_instance", "start_s": 0, "end_s": 600,
                    "start_unix_ms": 1_000_000, "end_unix_ms": 1_600_000,
                    "instances": ["checkout-2"], "set": {}, "expect": {}}],
        "expect": {},
    }  # fmt: skip
    t = load_truth(gt)
    assert t.no_onset and t.expected_sources == (MEASUREMENT,)
    assert t.origin == ("checkout-2",) and t.control == ("checkout-0", "checkout-1")
    assert "checkout" not in t.entities  # a subset fault: the service is not one entity
    assert t.root_cause_terms[0] == "checkout-2"


def test_not_a_truth_file():
    with pytest.raises(ValueError):
        load_truth({"hello": 1})


# --- text and selector helpers


@pytest.mark.parametrize(
    "text,entity,hit",
    [
        ("payment errors rose", "payment", True),
        ("the paymentservice fails", "payment", True),
        ("paymentFailure flag", "payment", False),
        ("product catalog latency", "product-catalog", True),
        ("productcatalog latency", "product-catalog", True),
        ("product_catalog latency", "product-catalog", True),
        ("POST /api/checkout fails", "checkout", True),
        ("checkout-2 has no gauge", "checkout-2", True),
        ("checkout-20 has no gauge", "checkout-2", False),
        ("the adservice", "ad", True),
        ("an address", "ad", False),
    ],
)
def test_mentions(text, entity, hit):
    assert mentions(text, entity) is hit


def test_coverage_of_selectors():
    t = truth("payment-failure")
    assert coverage('x{service_name="payment"}', t) == ({"payment"}, True)
    assert coverage('x{service_name=~"checkout|frontend"}', t) == ({"checkout", "frontend"}, True)
    got, _ = coverage('x{service_name!="cart"}', t)
    assert "cart" not in got and "payment" in got
    assert coverage("sum by (service_name) (rate(x[2m]))", t)[0] == set(t.entities)
    assert coverage("sum(rate(x[2m]))", t) == (set(), False)
    assert coverage('x{job="otel-demo/payment"}', t)[0] == {"payment"}
    assert coverage('x{service_name=~"(unclosed"}', t) == (set(), True)  # bad regex: nothing


# --- the good investigation passes everything


def test_good_payment_investigation_passes():
    rep = score(snap("payment-failure"), truth("payment-failure"))
    bad = [c for c in rep.checks if c.status == "fail"]
    assert not bad, bad
    assert rep.acceptance and rep.score == 1.0 and rep.unscoped_claims == []
    assert {a.id: a.verdict for a in rep.annotations} == {
        "a1": "onset_ok", "a2": "baseline", "a3": "end_ok",
    }  # fmt: skip
    roles = {h.id: h.role for h in rep.hypotheses}
    assert roles == {"h1": "root_cause", "h2": "blames_control"}


def test_good_queue_sim_investigation_passes():
    rep = score(snap("overload_spike"), truth("overload_spike"))
    assert rep.acceptance and rep.score == 1.0
    assert status(rep, "no_control_blamed") == "n/a"  # every instance overloaded: no control


def test_bare_workspace_body_is_accepted():
    s = snap("payment-failure")
    rep = score(s["workspace"], truth("payment-failure"))  # no exprs, no transcript
    assert status(rep, "findings_evidenced") == "pass"


# --- the flawed investigation fails the right criteria


def test_flawed_homepage_investigation():
    rep = score(snap("homepage-flood"), truth("homepage-flood"))
    st = {c.id: c.status for c in rep.checks}
    assert st["findings_scoped"] == "fail" and rep.check("findings_scoped").objects == ["f2"]
    assert st["findings_evidenced"] == "fail"  # f2 cites only an annotation
    assert st["no_control_blamed"] == "fail"  # payment blamed, h1 supported
    assert st["source_label"] == "fail"  # the flood labelled common cause
    assert st["annotation_onset"] == "fail"  # 277 s early, tolerance 150 s
    assert st["root_cause_named"] == "fail"  # no finding names the load generator
    assert st["root_cause_hypothesis_supported"] == "fail"  # h2 only proposed
    assert st["zero_unscoped_claims"] == "fail"
    assert [u["where"] for u in rep.unscoped_claims] == ["f2", "transcript"]
    assert not rep.acceptance
    assert {h.id: h.role for h in rep.hypotheses} == {"h1": "blames_control", "h2": "root_cause"}
    assert st["decoys_not_supported"] == "fail"


def test_no_findings_fails_acceptance():
    s = snap("payment-failure")
    s["workspace"]["findings"] = []
    rep = score(s, truth("payment-failure"))
    assert status(rep, "findings_present") == "fail"
    assert status(rep, "findings_scoped") == "fail" and not rep.acceptance
    assert status(rep, "uncertainty_honest") == "n/a"


# --- single-criterion mutations of the good snapshot


def mutate(fn) -> object:
    s = copy.deepcopy(snap("payment-failure"))
    fn(s)
    return score(s, truth("payment-failure"))


def test_claim_beyond_selector_is_unscoped():
    def m(s):
        finding(s, "f1")["claim"] += " checkout failed for the same reason."

    rep = mutate(m)
    f1 = next(f for f in rep.findings if f.id == "f1")
    assert f1.uncovered == ["checkout"] and not f1.scoped
    assert status(rep, "zero_unscoped_claims") == "fail"


def test_evidence_covers_what_the_selector_does_not():
    def m(s):
        f = finding(s, "f1")
        f["claim"] += " checkout errors moved with it."
        f["evidence"].append({"kind": "panel", "panel": "p1"})  # p1 is by service_name

    assert status(mutate(m), "findings_scoped") == "pass"


def test_unrestricted_selector_naming_a_service_is_unscoped():
    def m(s):
        f = finding(s, "f1")
        f["scope"]["selector"] = "traces_span_metrics_calls_total"
        f["evidence"] = [{"kind": "panel", "panel": "p2"}]
        s["exprs"]["p2"] = ["sum(rate(traces_span_metrics_calls_total[2m]))"]

    rep = mutate(m)
    assert status(rep, "findings_scoped") == "fail"


def test_time_range_outside_run_is_not_scoped():
    def m(s):
        finding(s, "f3")["scope"]["time_range"] = {"start_ms": 1_000_000, "end_ms": 2_000_000}

    rep = mutate(m)
    assert rep.check("findings_scoped").objects == ["f3"]


def test_missing_scope_field():
    def m(s):
        finding(s, "f1")["scope"]["aggregation"] = ""

    assert status(mutate(m), "findings_scoped") == "fail"


def test_dangling_panel_and_annotation_only_evidence():
    def m(s):
        finding(s, "f1")["evidence"] = [{"kind": "panel", "panel": "p99"}]
        finding(s, "f3")["evidence"] = [{"kind": "annotation", "annotation": "a1"}]

    rep = mutate(m)
    assert rep.check("findings_evidenced").objects == ["f1", "f3"]


def test_unknown_uncertainty_must_be_said():
    def m(s):
        finding(s, "f2")["caveats"] = []

    rep = mutate(m)
    assert rep.check("uncertainty_honest").objects == ["f2"]


def test_blaming_a_control_in_a_finding():
    def m(s):
        finding(s, "f1")["claim"] += " The root cause is the cart service."

    rep = mutate(m)
    assert status(rep, "no_control_blamed") == "fail"


def test_naming_a_control_while_ruling_it_out_is_fine():
    def m(s):
        finding(s, "f1")["claim"] += " cart is not the cause: its errors stayed at zero."

    assert status(mutate(m), "no_control_blamed") == "pass"


def test_change_asserted_on_control_only_scope_blames_it():
    def m(s):
        f = finding(s, "f3")
        f["claim"] = "cart error spans increased during the window."
        f["stance"] = f["hypothesis"] = None

    rep = mutate(m)
    assert status(rep, "no_control_blamed") == "fail"
    assert status(rep, "directions_consistent") == "fail"


def test_wrong_direction():
    def m(s):
        finding(s, "f1")["claim"] = "payment error spans dropped to 0.003/s after 10:08Z."

    assert status(mutate(m), "directions_consistent") == "fail"


def test_supported_control_hypothesis_fails():
    def m(s):
        s["workspace"]["hypotheses"][1]["status"] = "supported"

    rep = mutate(m)
    assert status(rep, "decoys_not_supported") == "fail"
    assert status(rep, "no_control_blamed") == "fail"


def test_proposed_decoy_is_unresolved():
    def m(s):
        s["workspace"]["hypotheses"][1]["status"] = "proposed"

    assert status(mutate(m), "decoys_not_supported") == "fail"


def test_root_cause_hypothesis_left_proposed():
    def m(s):
        s["workspace"]["hypotheses"][0]["status"] = "proposed"

    assert status(mutate(m), "root_cause_hypothesis_supported") == "fail"


def test_common_cause_on_the_incident_is_wrong():
    def m(s):
        finding(s, "f1")["evidence"][0]["source"] = "common_cause"
        finding(s, "f2")["evidence"][0]["source"] = "common_cause"

    rep = mutate(m)
    assert status(rep, "source_label") == "fail"


def test_unlabelled_incident_findings_fail_source_label():
    def m(s):
        for fid in ("f1", "f2"):
            finding(s, fid)["evidence"][0].pop("source")

    assert status(mutate(m), "source_label") == "fail"


@pytest.mark.parametrize(
    "delta_s,verdict",
    [(0, "onset_ok"), (149, "onset_ok"), (-149, "onset_ok"), (-151, "off"), (290, "end_ok")],
)
def test_annotation_tolerance(delta_s, verdict):
    t = truth("payment-failure")

    def m(s):
        s["workspace"]["annotations"] = [
            {"id": "a1", "kind": "event", "panel": "p1", "label": "onset",
             "t_start_ms": t.fault_start_ms + delta_s * 1000, "t_end_ms": None,
             "author": "claude", "deleted": False}
        ]  # fmt: skip

    rep = mutate(m)
    assert rep.annotations[0].verdict == verdict
    assert status(rep, "annotation_onset") == ("pass" if verdict == "onset_ok" else "fail")


def test_user_and_deleted_annotations_are_ignored():
    def m(s):
        for a in s["workspace"]["annotations"]:
            a["author"] = "user"

    rep = mutate(m)
    assert rep.annotations == [] and status(rep, "annotation_onset") == "fail"


def test_off_target_annotation_fails_even_with_a_good_onset():
    t = truth("payment-failure")

    def m(s):
        s["workspace"]["annotations"].append(
            {"id": "a9", "kind": "event", "label": "spike", "author": "claude",
             "t_start_ms": t.fault_end_ms + 900_000, "t_end_ms": None, "deleted": False}
        )  # fmt: skip

    rep = mutate(m)
    assert (
        status(rep, "annotation_onset") == "fail" and "a9" in rep.check("annotation_onset").objects
    )


@pytest.mark.parametrize(
    "text,flagged",
    [
        ("The checkout errors were caused by payment failing.", True),
        ("The checkout errors were caused by payment failing (f1, h1).", False),
        ("Payment might be the root cause; untested.", False),
        ("Errors rose at 10:08Z.", False),  # no entity
        ("Payment errors rose at 10:08Z.", False),  # no causal wording
        # the analyst's own limit, not a cause (live run 2026-10-03T13:17Z)
        (
            (
                "I cannot confirm they are order placement, because frontend metrics carry no "
                "route label."
            ),
            False,
        ),
        ("I could not link the frontend 422s to checkout because no route label exists.", False),
        ("Frontend failed because checkout could not reach payment.", True),
        # an entity said to be absent from the data, citing nothing (same run: false)
        ("There is no checkout or payment service, and no 5xx series anywhere.", True),
        ("Only frontend, cart, shipping and ad emit metrics; payment does not emit any.", True),
        ("No checkout or payment series in d3 (g1).", False),  # cites what it looked at
        ("Payment metrics may be absent from this source.", False),  # hedged
    ],
)
def test_transcript_unscoped(text, flagged):
    assert bool(transcript_unscoped(text, truth("payment-failure"))) is flagged


def test_markdown_report():
    rep = score(snap("homepage-flood"), truth("homepage-flood"))
    md = markdown(rep, {"model": "sonnet", "total_cost_usd": 0.5})
    assert "# Scenario eval: homepage-flood" in md and "| annotation_onset * | FAIL |" in md
    assert "## Unscoped claims" in md and "total_cost_usd: 0.5" in md
    assert json.loads(json.dumps(rep.to_dict()))["acceptance"] is False


# --- questions never give the answer away


@pytest.mark.parametrize("name", sorted(DEMO))
def test_questions_are_neutral(name):
    t = truth(name)
    q = question_for(t).lower()
    assert "utc" in q
    for word in (t.raw["root_cause"]["flag"].lower(), *t.root_cause_terms):
        assert not mentions(q, word) if word in t.entities else word.lower() not in q, word
    for e in t.entities:
        assert not mentions(q, e), e


def test_queue_sim_question():
    q = question_for(truth("overload_spike"))
    assert "checkout" in q and "overload" not in q.lower()


# --- live helpers (pure parts)


def test_claude_command_is_isolated(tmp_path):
    cmd = live.claude_command("/telemetry-nerd:investigate q", tmp_path, max_turns=12)
    assert cmd[1:3] == ["-p", "/telemetry-nerd:investigate q"]
    i = cmd.index("--allowedTools")
    allowed = cmd[i + 1 : cmd.index("--disallowedTools")]
    assert live.PLUGIN_MCP in allowed and "Bash" not in allowed
    denied = cmd[cmd.index("--disallowedTools") + 1 :]
    assert {"Bash", "Read", "Write", "WebFetch"} <= set(denied)
    assert cmd[cmd.index("--max-turns") + 1] == "12"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--max-budget-usd" in cmd and "stream-json" in cmd


def test_stage_plugin_copies_no_ground_truth(tmp_path):
    root = Path(__file__).resolve().parents[2]
    dest = live.stage_plugin(root, tmp_path / "plugin")
    assert (dest / ".claude-plugin" / "plugin.json").exists()
    assert (dest / "commands" / "investigate.md").exists()
    assert (dest / "scripts" / "tn-launch").exists()
    assert not (dest / "tests").exists() and not (dest / "scenarios").exists()


def test_claude_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TN_USE_SOURCE", "1")
    env = live.claude_env(tmp_path, "http://127.0.0.1:1234")
    assert env["TN_DAEMON_URL"] == "http://127.0.0.1:1234" and "TN_USE_SOURCE" not in env
    assert env["PATH"].startswith(str(tmp_path / ".venv" / "bin"))


def test_parse_stream():
    lines = [
        json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet",
                    "tools": ["Skill"], "mcp_servers": [
                        {"name": "plugin:telemetry-nerd:telemetry-nerd", "status": "connected"}]}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": f"{live.PLUGIN_MCP}__query", "input": {}},
            {"type": "tool_use", "name": "Bash", "input": {}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "denied"}]}}),
        "not json",
        json.dumps({"type": "result", "result": "done (f1)", "total_cost_usd": 0.42,
                    "num_turns": 7, "is_error": False}),
    ]  # fmt: skip
    out = live.parse_stream(lines)
    assert out["result"] == "done (f1)" and out["total_cost_usd"] == 0.42
    assert out["tool_counts"] == {f"{live.PLUGIN_MCP}__query": 1, "Bash": 1}
    assert out["outside_plugin"] == ["Bash"] and out["errors"] == ["denied"]
    assert live.mcp_connected(out["mcp_servers"])
    assert not live.mcp_connected([{"name": "plugin:telemetry-nerd:telemetry-nerd",
                                    "status": "failed"}])  # fmt: skip


def test_exprs_from_event_payloads():
    events = [
        {"seq": 1, "payload": {"dataset": "ds1", "expr": "rate(x[1m])"}},
        {"seq": 2, "payload": {"panel": {"id": "p1", "expr": "sum(y)"}}},
        {"seq": 3, "payload": {"dataset": "ds1", "expr": "rate(x[1m])"}},
    ]
    assert live.exprs_from(events) == {"ds1": ["rate(x[1m])"], "p1": ["sum(y)"]}


def test_failing_counts_as_a_change_and_needs_a_source_label():
    def m(s):
        f = finding(s, "f1")
        f["claim"] = "payment Charge calls failed on every call 10:08-10:12Z."
        f["evidence"] = [{"kind": "panel", "panel": "p2"}]  # no statistic: no source label
        finding(s, "f2")["evidence"][0].pop("source")

    rep = mutate(m)
    assert next(f for f in rep.findings if f.id == "f1").incident
    assert status(rep, "source_label") == "fail"


def test_real_sonnet_run_scores_as_observed():
    """The first live run (bead d77.3): the score is a finding about the product."""
    t = load_truth(EV / "payment-failure.live-sonnet.truth.json")
    rep = score(snap("payment-failure.live-sonnet"), t)
    st = {c.id: c.status for c in rep.checks}
    assert st["root_cause_named"] == "pass" and st["no_control_blamed"] == "pass"
    assert st["findings_evidenced"] == "pass"
    assert st["findings_scoped"] == "fail"  # f1 names checkout/frontend, cites payment only
    assert rep.findings[0].uncovered == ["checkout", "frontend"]
    assert st["annotation_onset"] == "fail" and rep.annotations == []
    assert st["root_cause_hypothesis_supported"] == "fail"  # h1 names no service
    assert st["supported_hypotheses_disciplined"] == "fail"  # h1: no alternative considered
    assert not rep.acceptance


def test_daemon_scope_and_source_verdicts_are_used():
    def m(s):
        f = finding(s, "f1")
        f["scope_check"] = {"status": "beyond_evidence", "not_covered": ['service_name="checkout"']}
        f["source_flags"] = [{"evidence": 0, "flag": "source_undetermined", "message": "x"}]

    rep = mutate(m)
    f1 = next(f for f in rep.findings if f.id == "f1")
    assert f1.uncovered == ["checkout"] and not f1.scoped
    assert "undetermined" in f1.sources


def test_supported_hypothesis_needs_an_alternative():
    assert status(mutate(lambda s: None), "supported_hypotheses_disciplined") == "pass"

    def m(s):
        for h in s["workspace"]["hypotheses"]:
            if h["id"] == "h2":
                h["status"] = "proposed"

    assert status(mutate(m), "supported_hypotheses_disciplined") == "fail"

    def noted(s):
        m(s)
        s["workspace"]["hypotheses"][0]["alternatives_considered"] = "checkout ruled out by f2"

    assert status(mutate(noted), "supported_hypotheses_disciplined") == "pass"


@pytest.mark.parametrize(
    "claim,incident",
    [
        # live run 2026-10-03T13:17Z: the change is in the first clause, the recovery after it
        (
            (
                "Frontend returned POST 422s from 13:24 to 13:28 UTC (peak ~0.23/s), then zero "
                "from 13:29 to the end of data."
            ),
            True,
        ),
        ("Checkout started returning 500s at 10:08Z.", True),
        ("Frontend returned 200s throughout; no errors.", False),
        ("Frontend errors stayed at zero, then remained flat.", False),
    ],
)
def test_incident_is_judged_per_clause(claim, incident):
    def m(s):
        f = finding(s, "f1")
        f["claim"] = claim
        f.pop("hypothesis", None)
        f["stance"] = None

    rep = mutate(m)
    assert next(f for f in rep.findings if f.id == "f1").incident is incident


def test_rerun_after_eval_fixes_scores_as_observed():
    """Second live run (d77.3, after 981/pxu/sat/qxp/icm): annotated, scoped and evidenced, but
    Claude never looked at span metrics (the only metrics payment emits), refuted a cart decoy,
    and told the user there is no checkout or payment service: the scorer catches that absence
    claim; the 'cannot confirm ... because' sentence is the analyst's own limit, not a cause."""
    t = load_truth(EV / "payment-failure.live-sonnet-2.truth.json")
    rep = score(snap("payment-failure.live-sonnet-2"), t)
    st = {c.id: c.status for c in rep.checks}
    assert {k for k, v in st.items() if v == "pass"} == {
        "findings_present", "findings_scoped", "findings_evidenced", "uncertainty_honest",
        "no_control_blamed", "directions_consistent", "annotation_onset", "decoys_not_supported",
    }  # fmt: skip
    assert {k for k, v in st.items() if v == "fail"} == {
        "root_cause_named", "source_label", "zero_unscoped_claims",
        "root_cause_hypothesis_supported",
    }  # fmt: skip
    assert st["supported_hypotheses_disciplined"] == "n/a"  # nothing was supported
    [u] = rep.unscoped_claims
    assert u["why"] == "absence claim citing no object" and "no checkout or payment" in u["claim"]
    f1 = next(f for f in rep.findings if f.id == "f1")
    assert f1.incident and f1.sources == ["undetermined"] and not f1.source_ok
    assert [a.verdict for a in rep.annotations] == ["onset_ok"]
    assert (rep.passed, rep.applicable) == (8, 12) and not rep.acceptance


def test_queue_sim_live_run_scores_as_observed():
    """Live queue-sim run (d77.3): Little's law checked and the episode annotated within
    tolerance; acceptance passes. No hypothesis about the spike's cause was opened (h1 framed
    the question, 'L exceeds λW', and was refuted), so root_cause_hypothesis_supported fails."""
    t = load_truth(EV / "overload_spike.live-sonnet.truth.json", extra_terms("overload_spike"))
    rep = score(snap("overload_spike.live-sonnet"), t)
    st = {c.id: c.status for c in rep.checks}
    assert [k for k, v in st.items() if v == "fail"] == ["root_cause_hypothesis_supported"]
    assert st["annotation_onset"] == st["zero_unscoped_claims"] == "pass"
    assert st["findings_scoped"] == st["findings_evidenced"] == "pass"
    f1 = rep.findings[0]
    assert f1.sources == ["measurement_system"] and not f1.incident
    assert [h.status for h in rep.hypotheses] == ["refuted"]
    assert (rep.passed, rep.applicable) == (8, 9) and rep.acceptance


def _qs_truth():
    return load_truth(EV / "overload_spike.live-sonnet-3.truth.json", extra_terms("overload_spike"))


@pytest.mark.parametrize(
    "statement,role",
    [
        # eval round 3, h2: the competing cause names the root-cause term only to negate it
        (("checkout: the episode is the checkout service itself slowing down (latency shift) at "
          "unchanged arrival rate, piling up in-flight requests"), "other"),
        ("checkout slowed; this is not an arrival surge", "other"),
        ("checkout: the episode is an arrival surge (request rate ~3x)", "root_cause"),
        ("an arrival surge, not a slowdown of the service", "root_cause"),
        ("no surge at first; then a surge in arrivals", "root_cause"),
    ],
)  # fmt: skip
def test_negated_root_cause_term_does_not_name_the_root_cause(statement, role):
    from telemetry_nerd.evals.score import score_hypothesis

    assert score_hypothesis({"id": "h1", "statement": statement}, _qs_truth()).role == role


def test_queue_sim_service_is_the_origin_and_the_sole_entity():
    """Every pod faulted: claims about 'checkout' are about the origin, and an expression over the
    one-service throwaway VM covers it unless a matcher picks pods or another service."""
    t = _qs_truth()
    assert "checkout" in t.implicated and t.sole == "checkout"
    assert coverage("sum(rate(http_requests_total[1m]))", t)[0] == {"checkout"}
    assert coverage('http_requests_total{service="checkout"}', t)[0] == {"checkout"}
    assert coverage('http_requests_total{service="cart"}', t)[0] == set()
    assert coverage('http_requests_total{pod="checkout-0"}', t)[0] == {"checkout-0"}
    assert "checkout" in coverage("sum by (pod) (http_server_active_requests)", t)[0]


def test_daemon_not_covered_is_overruled_only_for_the_sole_entity_it_covers():
    s = copy.deepcopy(snap("overload_spike.live-sonnet-3"))
    rep = score(s, _qs_truth())
    f3 = next(f for f in rep.findings if f.id == "f3")
    assert f3.scoped and f3.uncovered == []  # pooled over the only service: covered
    finding(s, "f3")["scope"]["selector"] = 'http_requests_total{pod="checkout-0"}'
    finding(s, "f3")["evidence"] = [{"kind": "statistic", "dataset": "dX", "name": "x",
                                     "value": 1, "source": "special_cause"}]  # fmt: skip
    s["exprs"]["dX"] = ['rate(http_requests_total{pod="checkout-0"}[1m])']
    f3 = next(f for f in score(s, _qs_truth()).findings if f.id == "f3")
    assert f3.uncovered == ["checkout"]  # one pod's evidence does not cover the service


def test_level_shift_counts_as_a_change():
    from telemetry_nerd.evals.score import asserts_change

    assert asserts_change("checkout mean latency level-shifted up by 15.5 s at 14:38:45")
    assert asserts_change("latency shifted down by 15 s at 14:40")


def test_queue_sim_round3_scores_as_observed():
    """Eval round 3, queue-sim (after rbz/t75/e0k/1w7/lep/zqa): Little's law consistent over the
    window (f1), the episode annotated +20 s from fault start, an arrival-surge hypothesis h1
    supported against a refuted slowdown h2 (t75 works). No finding names the surge: f3 says the
    request rate rose ~3x but calls the counter 'likely completion-side' and cites a run_code
    peak with no source of variation (source_label fails). 14 tool rounds, num_turns 32."""
    rep = score(snap("overload_spike.live-sonnet-3"), _qs_truth())
    st = {c.id: c.status for c in rep.checks}
    assert {k for k, v in st.items() if v == "fail"} == {"root_cause_named", "source_label"}
    assert {k for k, v in st.items() if v == "n/a"} == {"no_control_blamed", "decoys_not_supported"}
    by = {f.id: f for f in rep.findings}
    assert by["f2"].incident and by["f2"].source_ok and by["f2"].sources == ["special_cause"]
    assert by["f3"].incident and by["f3"].sources == ["undetermined"] and not by["f3"].source_ok
    assert [(h.id, h.role, h.status) for h in rep.hypotheses] == [
        ("h1", "root_cause", "supported"), ("h2", "other", "refuted")]  # fmt: skip
    assert [a.verdict for a in rep.annotations] == ["onset_ok"]
    assert (rep.passed, rep.applicable) == (9, 11) and rep.acceptance


def test_queue_sim_round3_stream_counts_rounds_not_turns():
    out = live.parse_stream(
        (EV / "overload_spike.live-sonnet-3.stream.jsonl").read_text().splitlines()
    )
    assert (out["tool_rounds"], out["num_turns"], out["tool_results"]) == (14, 32, 29)
    assert live.caps({**out, "aborted": None}, 40, 5.0)["stopped_by"] == "finished"


_HEADLINE = (
    "Payment's Charge call failed from 14:53 to 14:57 UTC, and that is why placing an order failed."
)


def _pf3():
    return load_truth(EV / "payment-failure.live-sonnet-3.truth.json")


@pytest.mark.parametrize(
    "text,flagged",
    [
        # eval round 3: the analyst's own limit, not a cause
        ("I could not find why payment failed, because span metrics don't show it.", []),
        # items listed under a heading of what is not known are open questions, not claims
        ("**Not established**\n- The root cause inside payment. The span metrics lack logs.", []),
        ("Unknowns:\n- why payment failed\n\nPayment broke because of a deploy.",
         ["Payment broke because of a deploy."]),
        # an uncited headline cause: 'that is why' states a cause
        (_HEADLINE, [_HEADLINE]),
        ("Payment's Charge call failed (h1, f1), and that is why placing an order failed.", []),
    ],
)  # fmt: skip
def test_transcript_limits_and_unknown_lists_are_not_claims(text, flagged):
    assert [s for s, _ in transcript_unscoped(text, _pf3())] == flagged


def test_negated_entity_does_not_name_the_root_cause():
    from telemetry_nerd.evals.score import score_hypothesis

    t = _pf3()
    h = {"id": "h2", "statement": "An arrival surge at frontend, not a payment fault, caused the "
         "14:53-14:57 UTC order failures."}  # fmt: skip
    assert score_hypothesis(h, t).role == "other"
    h["statement"] = "payment Charge failed, not checkout"
    assert score_hypothesis(h, t).role == "root_cause"


def test_incident_entity_per_sentence_change_per_clause():
    """f1 of round 3: payment is named in a parenthesised list, the rise comes clauses later,
    then 'and were zero' (flat wording) in the same sentence."""
    rep = score(snap("payment-failure.live-sonnet-3"), _pf3())
    f1 = next(f for f in rep.findings if f.id == "f1")
    assert f1.incident and f1.sources == ["undetermined"] and f1.source_ok is False


def test_payment_round3_scores_as_observed():
    """Eval round 3, payment-failure (after rbz/t75/e0k/1w7/lep/zqa): entities + RED on span
    metrics found payment; h1 (payment Charge failed, propagated to checkout/frontend) supported
    against a refuted arrival-surge h2; region a1 +84 s from fault start. Fails: f1/f2 cite only
    panels (show_binding crashed, gzrz: no op statistic, source undetermined), and the answer
    opens with an uncited causal sentence."""
    rep = score(snap("payment-failure.live-sonnet-3"), _pf3())
    st = {c.id: c.status for c in rep.checks}
    assert {k for k, v in st.items() if v == "fail"} == {"source_label", "zero_unscoped_claims"}
    assert st["root_cause_named"] == st["root_cause_hypothesis_supported"] == "pass"
    assert st["annotation_onset"] == st["findings_scoped"] == st["findings_evidenced"] == "pass"
    [u] = rep.unscoped_claims
    assert u["claim"].startswith("Payment's Charge call failed") and "that is why" in u["claim"]
    assert [(h.id, h.role, h.status) for h in rep.hypotheses] == [
        ("h1", "root_cause", "supported"), ("h2", "other", "refuted")]  # fmt: skip
    assert [a.verdict for a in rep.annotations] == ["onset_ok"]
    assert (rep.passed, rep.applicable) == (10, 12) and not rep.acceptance


def test_payment_round3_stream_counts_rounds_not_turns():
    lines = (EV / "payment-failure.live-sonnet-3.stream.jsonl").read_text().splitlines()
    out = live.parse_stream(lines)
    assert (out["tool_rounds"], out["num_turns"], out["tool_results"]) == (22, 33, 31)


def _ss3():
    return load_truth(EV / "shipping-slowdown.live-sonnet-3.truth.json")


def test_hypothesis_whose_subject_is_a_control_blames_it():
    from telemetry_nerd.evals.score import score_hypothesis

    t = _ss3()
    h = {"id": "h2", "statement": "payment service Charge calls were the slow dependency of "
         "checkout PlaceOrder in 15:00-15:15 UTC"}  # fmt: skip
    assert score_hypothesis(h, t).role == "blames_control"
    h["statement"] = "checkout PlaceOrder slowed on its own, not payment"
    assert score_hypothesis(h, t).role == "other"


def test_shipping_round3_scores_as_observed():
    """Eval round 3, shipping-slowdown (latency): RED on span metrics, per-span latency found
    POST /ship-order; h1 (shipping slow, checkout inherited it) supported against a refuted
    payment decoy h2 (f2: checkout's Charge client span flat); region a1 +27 s. Fails only
    source_label: f1 cites a panel (show_binding crashed again, gzrz), source undetermined."""
    rep = score(snap("shipping-slowdown.live-sonnet-3"), _ss3())
    st = {c.id: c.status for c in rep.checks}
    assert [k for k, v in st.items() if v == "fail"] == ["source_label"]
    assert st["decoys_not_supported"] == "pass"
    assert [(h.id, h.role, h.status) for h in rep.hypotheses] == [
        ("h1", "root_cause", "supported"), ("h2", "blames_control", "refuted")]  # fmt: skip
    assert [a.verdict for a in rep.annotations] == ["onset_ok"]
    assert (rep.passed, rep.applicable) == (12, 13) and rep.acceptance


def test_shipping_round3_stream_counts_rounds_not_turns():
    lines = (EV / "shipping-slowdown.live-sonnet-3.stream.jsonl").read_text().splitlines()
    out = live.parse_stream(lines)
    assert (out["tool_rounds"], out["num_turns"]) == (31, 46)
    assert live.caps({**out, "aborted": None}, 40, 5.0)["turns_exceeded"] is False


# --- turn and budget caps (zqa) ---------------------------------------------------------------


def _assistant(mid, *names, text=None):
    content = [{"type": "tool_use", "name": n, "input": {}} for n in names]
    if text:
        content.append({"type": "text", "text": text})
    # stream-json emits each content block of one response as its own event, same message id
    return [json.dumps({"type": "assistant", "message": {"id": mid, "content": [c]}})
            for c in content]  # fmt: skip


def _results(k):
    return [json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]}}) for i in range(k)]  # fmt: skip


def _stream(rounds: list[int], num_turns: int, subtype="success", cost=0.5) -> list[str]:
    lines = []
    for i, k in enumerate(rounds):
        lines += _assistant(f"msg_{i}", *[f"{live.PLUGIN_MCP}__query"] * k) + _results(k)
    lines += _assistant("msg_last", text="done")
    lines.append(json.dumps({"type": "result", "subtype": subtype, "num_turns": num_turns,
                             "total_cost_usd": cost, "result": "done"}))  # fmt: skip
    return lines


def test_num_turns_is_prompt_plus_tool_results_not_rounds():
    """The payment run: 46 num_turns with --max-turns 40 and subtype success. Claude Code's
    num_turns counted 1 + 45 tool results; with parallel calls the tool-use rounds that
    --max-turns limits are fewer. Here 45 results in 38 rounds (7 rounds of two calls)."""
    out = live.parse_stream(_stream([2] * 7 + [1] * 31, num_turns=46))
    assert out["tool_results"] == 45 and out["num_turns"] == 1 + out["tool_results"]
    assert out["tool_rounds"] == 38 and out["assistant_messages"] == 39
    c = live.caps({**out, "aborted": None}, max_turns=40, max_budget_usd=5.0)
    assert c["turns_exceeded"] is False and c["budget_exceeded"] is False
    assert (c["tool_rounds"], c["num_turns_reported"], c["stopped_by"]) == (38, 46, "finished")


@pytest.mark.parametrize(
    ("run", "stopped"),
    [
        ({"subtype": "error_max_turns"}, "max_turns"),
        ({"subtype": "error_max_budget_usd"}, "max_budget"),
        ({"aborted": "turn cap: 41 tool-use rounds > max_turns 40"}, "harness"),
        ({"subtype": "success"}, "finished"),
    ],
)
def test_caps_say_what_stopped_the_run(run, stopped):
    assert live.caps(run, 40, 5.0)["stopped_by"] == stopped


def test_caps_flag_exceeded_turns_and_budget():
    c = live.caps({"tool_rounds": 41, "total_cost_usd": 5.2}, 40, 5.0)
    assert c["turns_exceeded"] and c["budget_exceeded"]
    assert live.caps({}, None, None)["turns_exceeded"] is None


def test_stream_without_message_ids_counts_each_tool_event_as_a_round():
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "x", "input": {}}]}})] * 3  # fmt: skip
    assert live.parse_stream(lines)["tool_rounds"] == 3


def test_report_shows_rounds_against_the_cap():
    c = live.caps({"tool_rounds": 41, "num_turns": 50, "total_cost_usd": 0.5}, 40, 5.0)
    rep = score(snap("payment-failure.live-sonnet-2"),
                load_truth(EV / "payment-failure.live-sonnet-2.truth.json"))  # fmt: skip
    md = markdown(rep, {"num_turns": 50, "caps": c})
    assert "tool rounds 41/40" in md and "TURN CAP EXCEEDED" in md


@pytest.mark.slow
def test_run_claude_stops_a_run_past_max_turns(tmp_path):
    """A fake `claude` that never stops calling tools: the harness kills it after max_turns
    rounds, whatever the CLI does with --max-turns."""
    lines = [json.dumps({"type": "system", "subtype": "init", "mcp_servers": [
        {"name": "plugin:telemetry-nerd:telemetry-nerd", "status": "connected"}]})]  # fmt: skip
    for i in range(10):
        lines += _assistant(f"m{i}", f"{live.PLUGIN_MCP}__query") + _results(1)
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import sys, time\n"
        f"for line in {lines!r}:\n"
        "    print(line, flush=True)\n"
        "    time.sleep(0.01)\n"
        "time.sleep(30)\n"
    )
    out = live.run_claude([sys.executable, str(script)], dict(os.environ), tmp_path,
                          tmp_path / "t.jsonl", timeout_s=20, max_turns=3, max_budget_usd=1.0)  # fmt: skip
    assert out["aborted"].startswith("turn cap: 4 tool-use rounds > max_turns 3")
    assert out["caps"]["stopped_by"] == "harness" and out["caps"]["turns_exceeded"]
    assert out["duration_s"] < 15


def test_scorer_reads_structured_hypothesis_links():
    """aiy: links come from `hypotheses` (the single hypothesis/stance of older snapshots too); a
    finding for h1 and against h2 is still an incident finding; a refuted alternative counts as
    considered only with a finding linked or a stated reason."""
    from telemetry_nerd.evals.score import against_only, links, ruled_out

    multi = {"hypotheses": [{"id": "h1", "stance": "for"}, {"id": "h2", "stance": "against"}]}
    assert links(multi) == multi["hypotheses"] and not against_only(multi)
    assert against_only({"hypotheses": [{"id": "h2", "stance": "against"}]})
    assert links({"hypothesis": "h2", "stance": "against"}) == [{"id": "h2", "stance": "against"}]
    assert against_only({"hypothesis": "h2", "stance": "against"}) and not against_only({})
    assert not ruled_out({"status": "refuted"})  # round 3's h2: a status alone
    assert ruled_out({"status": "refuted", "evidence_against": ["f3"]})
    assert ruled_out({"status": "inconclusive", "status_reason": "no arrivals counter"})
    assert not ruled_out({"status": "proposed", "evidence_against": ["f3"]})
