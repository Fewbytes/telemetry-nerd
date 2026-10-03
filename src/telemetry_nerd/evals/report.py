"""Render a scored eval as markdown (JSON is Report.to_dict)."""

from __future__ import annotations

from datetime import UTC, datetime

from telemetry_nerd.evals.score import ACCEPTANCE, Report

_MARK = {"pass": "PASS", "fail": "FAIL", "n/a": "n/a"}


def _t(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%H:%M:%SZ")


def _cell(s: str, n: int = 140) -> str:
    s = " ".join(str(s).split()).replace("|", "\\|")
    return s if len(s) <= n else s[: n - 1] + "…"


def markdown(r: Report, run: dict | None = None) -> str:
    out = [f"# Scenario eval: {r.scenario} ({r.kind})", ""]
    acc = "PASS" if r.acceptance else "FAIL"
    out.append(
        f"**Score {r.passed}/{r.applicable} ({r.score:.0%})** · d77 acceptance "
        f"({', '.join(ACCEPTANCE)}): **{acc}** · unscoped claims: {len(r.unscoped_claims)}"
    )
    fw = r.meta.get("fault_window_ms") or [None, None]
    tol = r.meta.get("tolerance_s") or ["?", "?"]
    out.append(
        f"Fault window {_t(fw[0])} - {_t(fw[1])} (tolerance -{tol[0]}s / +{tol[1]}s); "
        f"root cause: {', '.join(r.meta.get('root_cause_terms', []))}; controls: "
        f"{', '.join(r.meta.get('controls', [])) or '-'}"
    )
    if run:
        bits = [f"{k}: {run[k]}" for k in ("model", "num_turns", "total_cost_usd", "duration_s",
                                            "stop_reason", "is_error") if k in run]  # fmt: skip
        if c := run.get("caps"):
            bits.append(
                f"tool rounds {c.get('tool_rounds')}/{c.get('max_turns')} (--max-turns; "
                f"num_turns counts prompt + tool results), cost {c.get('cost_usd')}/"
                f"{c.get('max_budget_usd')} USD, stopped by {c.get('stopped_by')}"
                + (" — TURN CAP EXCEEDED" if c.get("turns_exceeded") else "")
                + (" — BUDGET EXCEEDED" if c.get("budget_exceeded") else "")
            )
        if bits:
            out.append("Run: " + "; ".join(bits))
    out += ["", "## Criteria", "", "| criterion | result | detail | objects |", "|---|---|---|---|"]
    for c in r.checks:
        star = " *" if c.id in ACCEPTANCE else ""
        out.append(
            f"| {c.id}{star} | {_MARK[c.status]} | {_cell(c.detail)} | {', '.join(c.objects)} |"
        )
    out += ["", "\\* d77 acceptance criterion", "", "## Findings", ""]
    if not r.findings:
        out.append("(none)")
    else:
        out += [
            "| id | scoped | evidenced | in window | root cause | source | claim | problems |",
            "|---|---|---|---|---|---|---|---|",
        ]
        yn = lambda b: "yes" if b else "NO"
        for f in r.findings:
            src = ",".join(f.sources) or "-"
            if f.source_ok is False:
                src += " (wrong)"
            out.append(
                f"| {f.id} | {yn(f.scoped)} | {yn(f.evidenced)} | {yn(f.in_window)} | "
                f"{'yes' if f.names_root_cause else '-'} | {src} | {_cell(f.claim, 160)} | "
                f"{_cell('; '.join(f.problems) or '-', 160)} |"
            )
    out += ["", "## Annotations", ""]
    if not r.annotations:
        out.append("(none)")
    else:
        out += ["| id | kind | start | end | onset delta | verdict | label |", "|---|---|---|---|---|---|---|"]  # fmt: skip
        for a in r.annotations:
            d = "-" if a.onset_delta_s is None else f"{a.onset_delta_s:+.0f}s"
            out.append(
                f"| {a.id} | {a.kind} | {_t(a.start_ms)} | {_t(a.end_ms)} | {d} | {a.verdict} | "
                f"{_cell(a.label, 80)} |"
            )
    out += ["", "## Hypotheses", ""]
    if not r.hypotheses:
        out.append("(none)")
    else:
        out += ["| id | role | status | for/against | statement |", "|---|---|---|---|---|"]
        for h in r.hypotheses:
            out.append(
                f"| {h.id} | {h.role} | {h.status} | {h.evidence_for}/{h.evidence_against} | "
                f"{_cell(h.statement, 160)} |"
            )
    if r.unscoped_claims:
        out += ["", "## Unscoped claims", ""]
        out += [
            f"- **{u['where']}**: {_cell(u['claim'], 220)} ({u['why']})" for u in r.unscoped_claims
        ]
    out += [
        "",
        (
            "Text rules (entity mentions, causal and direction wording) are heuristics; each "
            "flag lists the sentence it fired on so a reader can overrule it."
        ),
        "",
    ]
    return "\n".join(out)
