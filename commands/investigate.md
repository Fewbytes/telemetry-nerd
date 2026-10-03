---
description: Investigate a question about your systems, evidence first
argument-hint: "<question, e.g. why did checkout p95 spike at 14:00?>"
allowed-tools: mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_get, mcp__plugin_telemetry-nerd_telemetry-nerd__source_list, mcp__plugin_telemetry-nerd_telemetry-nerd__source_learn, mcp__plugin_telemetry-nerd_telemetry-nerd__hypothesis_create, mcp__plugin_telemetry-nerd_telemetry-nerd__hypothesis_update, mcp__plugin_telemetry-nerd_telemetry-nerd__finding_create, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_search, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_family, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_relations, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_suggest, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_accept, mcp__plugin_telemetry-nerd_telemetry-nerd__show_binding, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_verdict, mcp__plugin_telemetry-nerd_telemetry-nerd__query, mcp__plugin_telemetry-nerd_telemetry-nerd__query_distribution, mcp__plugin_telemetry-nerd_telemetry-nerd__fraction_over, mcp__plugin_telemetry-nerd_telemetry-nerd__show, mcp__plugin_telemetry-nerd_telemetry-nerd__show_marginal, mcp__plugin_telemetry-nerd_telemetry-nerd__analyze, mcp__plugin_telemetry-nerd_telemetry-nerd__compare_seasonal, mcp__plugin_telemetry-nerd_telemetry-nerd__fleet, mcp__plugin_telemetry-nerd_telemetry-nerd__spectrum, mcp__plugin_telemetry-nerd_telemetry-nerd__operating_profile, mcp__plugin_telemetry-nerd_telemetry-nerd__check_littles_law, mcp__plugin_telemetry-nerd_telemetry-nerd__split_outcome, mcp__plugin_telemetry-nerd_telemetry-nerd__run_code, mcp__plugin_telemetry-nerd_telemetry-nerd__code_get, mcp__plugin_telemetry-nerd_telemetry-nerd__annotate, mcp__plugin_telemetry-nerd_telemetry-nerd__gap_create, mcp__plugin_telemetry-nerd_telemetry-nerd__reply
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
   **Annotate as you go** (the user reads the chart, not the log): once the evidence places the
   onset, `annotate(kind="event", at=<onset>, panel=<the panel that shows it>, label="onset: ...")`;
   for a fault, outage or degradation window with a start and an end, `annotate(kind="region",
   at=<start>, until=<end>, panel=..., label=...)`, and mark the recovery (or "ongoing"). Use the
   onset interval's best estimate and put its bounds in the label. Annotate deploys, config
   changes and other events the user gave you the same way. Ask the user for such events once;
   if there are none, say so.
5. **Never claim without evidence.** (A signal you wish existed: `gap_create`.) A claim becomes `finding_create` with scope (source,
   selector, range, step) and evidence (cite the annotation id for an onset or window), and attaches to a hypothesis with its stance. Refuting
   evidence is recorded too. If the data cannot answer (gaps, settling data, too few samples),
   say so as the finding.
6. **Report** in a few lines: scope, hypotheses with status, findings with ids (f1, p3 ...) and
   what remains unexplained or unmeasurable. Share the workspace URL.
