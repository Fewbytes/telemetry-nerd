---
description: Investigate a question about your systems, evidence first
argument-hint: "<question, e.g. why did checkout p95 spike at 14:00?>"
allowed-tools: mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_get, mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_create, mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_list, mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_switch, mcp__plugin_telemetry-nerd_telemetry-nerd__source_list, mcp__plugin_telemetry-nerd_telemetry-nerd__source_learn, mcp__plugin_telemetry-nerd_telemetry-nerd__hypothesis_create, mcp__plugin_telemetry-nerd_telemetry-nerd__hypothesis_update, mcp__plugin_telemetry-nerd_telemetry-nerd__finding_create, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_search, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_family, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_relations, mcp__plugin_telemetry-nerd_telemetry-nerd__entities, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_suggest, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_accept, mcp__plugin_telemetry-nerd_telemetry-nerd__show_binding, mcp__plugin_telemetry-nerd_telemetry-nerd__binding_verdict, mcp__plugin_telemetry-nerd_telemetry-nerd__query, mcp__plugin_telemetry-nerd_telemetry-nerd__query_distribution, mcp__plugin_telemetry-nerd_telemetry-nerd__fraction_over, mcp__plugin_telemetry-nerd_telemetry-nerd__show, mcp__plugin_telemetry-nerd_telemetry-nerd__show_marginal, mcp__plugin_telemetry-nerd_telemetry-nerd__analyze, mcp__plugin_telemetry-nerd_telemetry-nerd__compare_seasonal, mcp__plugin_telemetry-nerd_telemetry-nerd__fleet, mcp__plugin_telemetry-nerd_telemetry-nerd__spectrum, mcp__plugin_telemetry-nerd_telemetry-nerd__operating_profile, mcp__plugin_telemetry-nerd_telemetry-nerd__check_littles_law, mcp__plugin_telemetry-nerd_telemetry-nerd__split_outcome, mcp__plugin_telemetry-nerd_telemetry-nerd__run_code, mcp__plugin_telemetry-nerd_telemetry-nerd__code_get, mcp__plugin_telemetry-nerd_telemetry-nerd__annotate, mcp__plugin_telemetry-nerd_telemetry-nerd__gap_create, mcp__plugin_telemetry-nerd_telemetry-nerd__reply
---

Investigate: `$ARGUMENTS`

If the question is empty, ask for one. Follow the `triage` skill for the method, the `evidence`
skill for what counts as a claim, and the `charting` skill for how to draw. The MCP server
instructions apply throughout.

1. **Orient.** `source_list` (if nothing is live, stop and point to `/telemetry-nerd:start`) and
   `workspace_get` (existing panels, hypotheses, open threads: build on them, do not repeat). If the active
   workspace holds a different investigation, call `workspace_create(title, question)` and say so
   in one line; the old one stays reopenable (`workspace_list`, `workspace_switch`).
2. **Scope the question** before touching data: the service or system, the time range (absolute
   times and timezone; a "spike" or "slow" needs a start, an end or "ongoing", and a baseline
   window), the symptom (latency, errors, saturation, throughput, missing data), and the source.
   Ask the user for whatever you cannot infer; at most one short round of questions. State the
   resulting scope in one line.
3. **Services first.** `entities(kind="service")`: which services exist, which metric families
   each reports, which binding_suggest ids they fill. Then `binding_suggest(kind="RED")` and query
   the RED binding that covers the most services, for all of them (span metrics,
   `traces_span_metrics_*`, cover traced services that emit no HTTP/RPC metrics). Never conclude
   that a service or signal is absent from an empty query or catalog lookup: cite the `entities`
   result and scope the claim to the labels and window it searched.
4. **Triage** per the `triage` skill: blast radius over those services, RED/USE per affected
   service, changepoints against the baseline, rule things out. Every panel answers an explicit
   question. A question you want to test up front may be a hypothesis (`hypothesis_create` with a
   concrete subject in the statement and its `scope`: `{selector, start, end, source?}`), but it
   does not replace step 5.
   **Annotate as you go** (the user reads the chart, not the log): once the evidence places the
   onset, `annotate(kind="event", at=<onset>, panel=<the panel that shows it>, label="onset: ...")`;
   for a fault, outage or degradation window with a start and an end, `annotate(kind="region",
   at=<start>, until=<end>, panel=..., label=...)`, and mark the recovery (or "ongoing"). Use the
   onset interval's best estimate and put its bounds in the label. Annotate deploys, config
   changes and other events the user gave you the same way. Ask the user for such events once;
   if there are none, say so.
5. **Cause hypotheses for the episode.** Once an episode is found (onset or window annotated),
   `hypothesis_create` a cause that names its concrete subject (the failing service, a flag, a
   deploy, an arrival surge vs. a slow service) and at least one competing cause, then test them:
   `finding_create(..., hypotheses=[{"id": <cause>, "stance": "for"}, {"id": <competitor>,
   "stance": "against"}])` (one observation that separates two causes is linked to both) and
   keep their status current with `hypothesis_update`. The `evidence` skill has the rules the
   server enforces: `supported` needs a concrete subject, a finding with stance=for and an
   alternative considered (another hypothesis refuted or inconclusive, or
   `alternatives_considered`); `refuted` needs a finding against it or a `reason`. A `hint` in
   finding_create's result means a special-cause finding's subject has no open hypothesis yet.
6. **Never claim without evidence.** (A signal you wish existed: `gap_create`.) A claim becomes
   `finding_create` with scope (source, selector, range, step) and evidence (for a change or
   deviation, the op's `evidence` statistic as returned, which carries its variation source; the
   panel and the annotation id for an onset or window beside it), and attaches to a hypothesis
   with its stance. Refuting evidence is recorded too. Name only the entities (services, pods,
   jobs) your cited evidence covers: a claim naming one outside it is refused with the datasets
   that hold it, so cite those; only when the claim must reach beyond its evidence pass
   `scope_note` saying why (the finding is flagged beyond_evidence). The result's `scope`
   (covered, beyond_evidence, undetermined) and `source_flags` are part of the finding: report
   them, and cite a matching `citable_statistics` entry rather than leave the source
   undetermined. If the data cannot answer (gaps, settling data, too few samples), say so as the
   finding.
7. **Report** in a few lines: scope, hypotheses with status, findings with ids (f1, p3 ...) with
   their scope status and source flags, and what remains unexplained or unmeasurable. Share the workspace URL.
