---
name: triage
description: This skill should be used when investigating a live or recent production problem with Telemetry Nerd: "the service is slow", "errors are up", "what's wrong with checkout", "investigate this alert", "what changed", "find the root cause", "is this incident real", "how bad is it / who is affected", or when running /tn:investigate. Gives the SRE incident flow step by step: symptom and window, blast radius, RED / USE per service, what moved first, changepoints, seasonal baseline, Little's law, hypotheses and ruling out, stop conditions, and the report.
---

# Incident triage

A short procedure. Each step names the tools; the methods live in sibling skills: `model-views`
(bindings, verdicts, Little's law), `evidence` (findings, hypotheses, uncertainty, sources of
variation), `charting` (which view answers which question), `metric-learning` (what a metric
means), `tier2-code` (custom statistics). Load them when a step needs them.

## 1. Symptom and window

- Restate the symptom as a measurable question: which signal (latency, errors, throughput,
  saturation), which service, since when, noticed how (alert, user report).
- Pick the window: from before the suspected onset to now, so the onset sits inside it (a
  change at the window edge has no onset). Note the reference: previous windows by default,
  the same hour of past days when the service has a daily rhythm.
- Decide the question before looking: every re-run with another range or reference is another
  look. Orient with `workspace_get`, `source_list`; `source_learn` once per source.

## 2. Blast radius

Which services, instances, regions carry the symptom?

- Query by the identifying label (`sum by (service_name) (...)`); for latency,
  `query_distribution(selector, by=[...])`, never per-member percentiles.
- Five or more members: `fleet(dataset)` names outliers (one bad pod) against the fleet's own
  spread. Every member moving together points away from a single instance.
- Read `coverage`, `unknown_spans`, `silent_members`: a member that stopped reporting may be the
  sick one (undetermined, not healthy).

## 3. Golden signals: what moved, what moved first

Per affected service, RED; per resource it waits on (CPU, pool, disk, node), USE. Follow the
`model-views` workflow: `binding_suggest` → `binding_accept` → `show_binding` →
`binding_verdict`. Report per role: changed or not, pattern, onset interval, the reference and
the family-wise alpha; `summary.first` only when onset intervals do not overlap. A RED change with
an unchanged rate argues against load; a USE role `at_capacity` argues for saturation.

## 4. Changepoints and one signal in depth

For a single signal: `analyze(dataset)` gives level shifts with onset intervals, drift, variance
change and the control chart (`show(dataset, question, mark="spc")`). `analyze(dataset,
baseline="day")` judges against yesterday. `spectrum` when the symptom repeats (cron, GC,
retries).

## 5. Seasonal baseline

Is now unusual for this hour? `compare_seasonal(dataset)` (latency from the histogram: share
above an edge per cycle), `operating_profile(expr)` for the learned normal,
`binding_verdict(..., reference="day")`, `show_marginal(panel, reference="week")`. A symptom
inside the seasonal band is the usual peak (common cause), not the incident.

## 6. Little's law where concurrency exists

A service with an in-flight gauge and a latency histogram: `check_littles_law(binding=...)`.
`L_high` means time outside the latency timer (queueing before it, stuck requests, latency on a
subset). Report the discrepancy first, then the verdict and assumptions (`model-views`).

## 7. Hypotheses and ruling out

Open competing hypotheses early with `hypothesis_create`; for each, look first for the
observation that would refute it. Attach results with `finding_create(...,
hypothesis=<id>, stance="for" | "against")` and move status with `hypothesis_update`. The cheap
alternatives to rule out first, and the test for each: `references/ruling-out.md`. Do not chase
common-cause points; separate measurement-system issues into their own findings.

## 8. Tier-2 code: the long tail only

`run_code` only when no tier-1 tool answers (an interval for a derived quantity, a custom model,
a join). See `tier2-code`.

## Stop conditions

Stop and report when any holds:

- **Localised**: a special cause with an onset interval and a scope (which members, which
  signal), and the cheap alternatives refuted or explicitly left open.
- **Normal**: the symptom is inside the common-cause envelope (seasonal band, control limits,
  fleet spread): say so with the envelope; the lever is the system, not a root cause.
- **Cannot tell**: the source is undetermined or a needed signal is missing: say what would
  separate the readings, and record it with `gap_create`.
- **Answered**: the user's question is answered at the precision asked; offer the next step
  instead of continuing unasked.

## Report

Short, in this order, with object ids (they become links): symptom and scope (service,
selector, window, reference, alpha); timeline (onsets with intervals, order only as far as
intervals allow); findings (`f1`...) with source labels and uncertainty flags; hypotheses with
status, including what was ruled out; unknowns and gaps; next steps. `highlight` the panel or
finding the user should open first. Never claim cause from ordering.

## Additional resources

- **`references/worked-example.md`**: an executed incident on a seeded scenario, symptom →
  binding → verdict → blast radius → ruled-out hypotheses → finding → report. Every call in it
  is run by the test suite.
- **`references/ruling-out.md`**: common hypotheses and the tool and reading that tests each.
