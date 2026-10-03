"""Scenario eval scoring (bead d77.3) on canned snapshots: no Claude, no demo, no daemon."""

from __future__ import annotations

import copy
import json
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
    assert t.origin == ("checkout-0", "checkout-1", "checkout-2") and t.control == ()
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
