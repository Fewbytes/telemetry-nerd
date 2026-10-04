---
description: Wrap up - propose catalog updates and scoped lessons from this investigation
argument-hint: "[optional focus, e.g. what to remember about checkout]"
allowed-tools: mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_get, mcp__plugin_telemetry-nerd_telemetry-nerd__workspace_activity, mcp__plugin_telemetry-nerd_telemetry-nerd__source_list, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_get, mcp__plugin_telemetry-nerd_telemetry-nerd__proposals_list, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_propose, mcp__plugin_telemetry-nerd_telemetry-nerd__lesson_propose, mcp__plugin_telemetry-nerd_telemetry-nerd__lessons_for, mcp__plugin_telemetry-nerd_telemetry-nerd__highlight
---

Session retrospective for the active workspace. Focus, if given: `$ARGUMENTS`.

You PROPOSE; the user decides in the UI (Proposals view). Nothing you propose reaches the
catalog or a later session until they approve it. Fewer, well-scoped proposals beat many.

1. **Review.** `workspace_get`: findings (with verdicts: a rejected finding is not evidence),
   hypotheses and their status, gaps. `proposals_list` shows what is already on file: never
   propose it again. If the workspace holds no finding and no panel, say there is nothing to
   wrap up and stop.
2. **Catalog proposals.** For each metric whose unit, type, bounds, role, description or
   thresholds the investigation actually established (and the catalog does not already hold:
   `catalog_get`), `catalog_propose(claims=[{metric, field, value, confidence, basis,
   evidence: [f…/p…]}], source)`. `basis` says what you checked; confidence at most 0.9. Not a
   guess, not something one time range merely suggested.
3. **Lessons.** At most three, each a methodology lesson that would have made this
   investigation faster or more correct next time ("for checkout, split latency by region
   before reading p95: one region carries the tail"), not a restatement of a finding.
   `lesson_propose(text, scope={source, service?, metric_family?, labels?}, evidence=[f…/p…])`.
   Scope it to what the evidence covers: evidence about one service makes a lesson about that
   service, never about the whole source; two services are not "all services". A refusal
   (`lesson_beyond_evidence`) lists what each item covers: narrow the scope, do not hunt for
   evidence to widen it. Give `expires` when the lesson depends on something likely to change
   (a deploy, a migration, a temporary config).
4. **Report** in a few lines: the proposals by id (cp…, ls…) with their scope, what you chose
   not to propose and why (too narrow evidence, already on file), and ask the user to review
   them in the UI's Proposals view (the workspace URL, `#/proposals`).
