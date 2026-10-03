---
description: Investigate a question about your systems, evidence first
argument-hint: "<question, e.g. why did checkout p95 spike at 14:00?>"
allowed-tools: mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_get, mcp__plugin_telemetry-nerd_telemetry-nerd__source_list, mcp__plugin_telemetry-nerd_telemetry-nerd__hypothesis_create, mcp__plugin_telemetry-nerd_telemetry-nerd__hypothesis_update, mcp__plugin_telemetry-nerd_telemetry-nerd__finding_create, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_search, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_suggest, mcp__plugin_telemetry-nerd_telemetry-nerd__query, mcp__plugin_telemetry-nerd_telemetry-nerd__show, mcp__plugin_telemetry-nerd_telemetry-nerd__gap_create, mcp__plugin_telemetry-nerd_telemetry-nerd__reply
---

Investigate: `$ARGUMENTS`

If the question is empty, ask for one. Follow the `triage` skill for the method, the `evidence`
skill for what counts as a claim, and the `charting` skill for how to draw. The MCP server
instructions apply throughout.

1. **Orient.** `source_list` (if nothing is live, stop and point to `/telemetry-nerd:start`) and
   `workspace_get` (existing panels, hypotheses, open threads: build on them, do not repeat).
2. **Scope the question** before touching data: the service or system, the time range (absolute
   times and timezone; a "spike" or "slow" needs a start, an end or "ongoing", and a baseline
   window), the symptom (latency, errors, saturation, throughput, missing data), and the source.
   Ask the user for whatever you cannot infer; at most one short round of questions. State the
   resulting scope in one line.
3. **Hypothesis.** `hypothesis_create` with a falsifiable statement that names the scope. The user
   sees it in the workspace; keep its status current with `hypothesis_update` as evidence arrives.
4. **Triage** per the `triage` skill: blast radius, RED/USE per service, changepoints against the
   baseline, rule things out. Every panel answers an explicit question.
5. **Never claim without evidence.** (A signal you wish existed: `gap_create`.) A claim becomes `finding_create` with scope (source,
   selector, range, step) and evidence, and attaches to a hypothesis with its stance. Refuting
   evidence is recorded too. If the data cannot answer (gaps, settling data, too few samples),
   say so as the finding.
6. **Report** in a few lines: scope, hypotheses with status, findings with ids (f1, p3 ...) and
   what remains unexplained or unmeasurable. Share the workspace URL.
