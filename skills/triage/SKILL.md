---
name: triage
description: This skill should be used when investigating a live or recent production problem with Telemetry Nerd: "the service is slow", "errors are up", "what's wrong with checkout", "investigate this alert", "what changed", "find the root cause", "is this incident real", "how bad is it / who is affected", or when running /telemetry-nerd:investigate. Gives the step-by-step SRE incident flow, stop conditions and the report.
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
  look (principle 14). Orient with `workspace_get`, `source_list`; `source_learn` once per source.
- Steps 2-5 may run in the order the symptom suggests; the worked example judges the golden
  signals first, then scopes the blast radius against the seasonal baseline.

## 2. Blast radius

Which services, instances, regions carry the symptom? Start from the services, not from the
first metric family you find.

- `entities(kind="service")` lists every service the source knows, the families each reports and
  the bindings its metrics fill. Then `binding_suggest(kind="RED")` and query the binding that
  covers the most services for all of them at once: span metrics (`traces_span_metrics_*`,
  `traces_spanmetrics_*`) cover traced services that emit no HTTP/RPC metrics of their own.
- An empty query or catalog lookup is absence of evidence. Never say a service or signal does
  not exist without an `entities` result (or series check) cited and scoped to its labels and
  window; otherwise "not found under these labels in this window".
- Query by the identifying label (`sum by (service_name) (...)`); for latency,
  `query_distribution(selector, by=[...])`: aggregate buckets, never average percentiles
  across members.
- Latency per member: `fraction_over(dataset, x, by_series=true)` or per-member
  `compare_seasonal(dataset)` on the distribution. Five or more members of a rate or share
  series (not a distribution): `fleet(dataset)` names outliers (one bad pod) against the
  fleet's own SPC band (median ± robust σ; flags from its family-wise tests, not the zones). Every member moving together points away from a single instance.
- Read `coverage`, `unknown_spans`, `silent_members`: a member with no samples since T may be the
  sick one (source undetermined, not healthy; "no samples since T", never "left": principle 9).

## 3. Golden signals: what moved, what moved first

Per affected service, RED; per resource it waits on (CPU, pool, disk, node), USE. Follow the
`model-views` workflow: check `catalog_relations(source, kind="RED")` for an existing binding,
else `binding_suggest` → `binding_accept`; then `show_binding` →
`binding_verdict(..., reference=)` with the reference chosen in step 1 (`auto` falls back to
`previous`, weak for daily rhythms). Report per role: changed or not, pattern, onset interval, the reference and
the family-wise alpha; `summary.first` only when onset intervals do not overlap. A RED change with
an unchanged rate argues against load; a USE role `at_capacity` argues for saturation.
Latency of a service that returns errors: `split_outcome(dataset)` separates failed from
successful requests (or say why it is deferred). Ask the user for deploy, config and dependency events near the onset; `annotate` them and
compare them with the onset interval (an event outside it does not explain the change).

## 4. Changepoints and one signal in depth

For a single signal (a dataset from `query`, or a rate or threshold count; histograms via
`query_distribution`), rates written `rate(x[$__rate_interval])` (a fixed `[1m]` spanning many
steps leaves few independent values, effective n): `analyze(dataset)` gives level shifts with onset intervals, drift, variance
change and the control chart (`show(dataset, question, mark="spc")`). `analyze(dataset,
baseline="day")` judges against yesterday. `spectrum` when the symptom repeats (cron, GC,
retries).

## 5. Seasonal baseline

Is now unusual for this hour? `compare_seasonal(dataset)` (latency from the histogram: share
above an edge per cycle), `operating_profile(expr)` for the learned normal,
`binding_verdict(..., reference="day")`, `show_marginal(panel, reference="week")`. A symptom
inside the seasonal band is the usual peak (common cause): it can still hurt users, but the
lever is capacity or the system, not a root cause in this window.

## 6. Little's law where concurrency exists

When concurrency exists (a `littles_law` binding, an in-flight gauge in the catalog, or a
`binding_suggest(source, kind="littles_law")` with the concurrency role filled) and latency is a
histogram: `check_littles_law(binding=...)`. No in-flight gauge: skip the check, never derive L,
and record the gap.
`L_high` means time outside the latency timer (queueing before it, stuck requests, latency on a
subset). Report the discrepancy first, then the verdict and assumptions (`model-views`).
A surge in L with Little's law consistent and λ up is an arrival-driven backlog: open it as a
cause hypothesis ("arrival surge") against "the service slowed" and separate them by onset order
in `binding_verdict` (λ moved first, or W).

## 7. Hypotheses and ruling out

Open competing hypotheses early with `hypothesis_create`, each naming the suspected service,
resource or metric ("payment `charge` calls fail", not "a fault in one service"); for each,
look first for the observation that would refute it. **Once an episode is found** (an onset,
an annotated window), open a cause hypothesis for it that names the concrete subject (the
failing service, a flag, a deploy, an arrival surge), plus at least one competing cause, and
test both: a question-framing or decoy hypothesis does not explain the episode.
`finding_create` returns a `hint` when a special-cause finding's subject has no open hypothesis. `supported` is refused until a finding
backs it and an alternative is refuted / inconclusive (or `alternatives_considered` says how
it was ruled out); `refuted` is refused until a finding is against it (or a `reason` says what
rules it out). Attach results with `finding_create(..., hypotheses=[{"id": <id>, "stance":
"for" | "against"}, ...])`, one link per hypothesis the observation bears on (finding anatomy,
scope and source labels: the `evidence` skill) and move status with `hypothesis_update`. The cheap
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
  fleet SPC band): say so with the envelope; the lever is the system, not a root cause.
- **Cannot tell**: the source is undetermined or a needed signal is missing: say what would
  separate the readings; record an undetermined source as a finding carrying its label, and a
  missing signal with `gap_create`.
- **Looks exhausted**: further re-runs with other ranges, references or alphas would only add
  looks the family alpha does not cover: report what is known and how many looks were taken.
- **Answered**: the user's question is answered at the precision asked; offer the next step
  instead of continuing unasked.

## Report

Short, in this order, with object ids (they become links): symptom and scope (service,
selector, window, reference, alpha); timeline (onsets with intervals, order only as far as
intervals allow); findings (`f1`...) with source labels and uncertainty flags; hypotheses with
status, including what was ruled out; unknowns and gaps; next steps. `highlight` the panel or
finding the user should open first. Never claim cause from ordering (principle 13). Results are
model outputs (principle 16): "under a Poisson model …", "consistent with …", never a bare fact;
a label stands only under the cautious model the op names. Principles:
`docs/principles.md`.

## Additional resources

- **`references/worked-example.md`**: an executed incident on a seeded scenario, symptom →
  binding → verdict → blast radius → ruled-out hypotheses → finding → report. Every call in it
  is run by the test suite.
- **`references/discovery-example.md`**: services first (`entities`), RED from span metrics over
  all of them, the failing service, a cause hypothesis with a refuted competitor. Run by the tests.
- **`references/ruling-out.md`**: common hypotheses and the tool and reading that tests each.
