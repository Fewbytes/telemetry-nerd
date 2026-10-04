---
name: evidence
description: This skill should be used when recording or stating any conclusion about telemetry in Telemetry Nerd: before calling finding_create, hypothesis_create, hypothesis_update or gap_create, when asked to "record a finding", "write up what we found", "is this evidence enough", "summarise the investigation", "rule this out", "what is the uncertainty", "is this a real change or noise", or when deciding whether a number, a verdict or a chart supports a claim. Covers scoping, uncertainty flags, sources of variation and ruling out.
---

# Evidence discipline

A claim in Telemetry Nerd is a **finding**: a scoped statement backed by evidence objects the
user can open. Hypotheses are explanations under test; gaps are signals that would be needed but
do not exist. The rules below are binding: correct over conventional, never what a dashboard
habit suggests. They apply the project's principles (`docs/principles.md`, cited by number).

## What counts as evidence (principle 1)

| Evidence item | Shape in `finding_create` | Counts when |
|---|---|---|
| statistic | `{kind: "statistic", dataset, name, value, method, params?, interval: [lo, hi] \| exact: true \| uncertainty_unknown: true, source?}` | it came from an op's `evidence` (pass it **as returned**) or a code output that declared its uncertainty |
| panel | `{kind: "panel", panel: "p3"}` | the panel answers the question the claim is about |
| annotation | `{kind: "annotation", annotation: "a2"}` | it marks the event or region the claim cites (an onset, a deploy) |
| catalog claim | `{kind: "claim", source, metric, field, origins: [two or more], note?}` | catalog origins disagree about a metric's field (unit, type): a measurement-system finding (see `metric-learning`) |

Not evidence: a thread reply, a number read off a chart by eye, a summary line without its
statistic, a percentile without its sample count, an averaged percentile, a fit tagged
`weak_fit`. Prefer the op's own `evidence` statistic over restating its numbers: it carries the
method, the interval, the dataset and the source label.

## Scope every claim (principle 2)

`scope` is required: `{source, selector, start, end, step, aggregation, baseline_start?,
baseline_end?}`. `step` is the dataset's resolved step from its summary (`1m`), never `auto`
(refused). Write the claim so it is true **only** inside that scope:

- **Service and selector**: the metric and label filter the numbers came from, not the service
  name in general. Two members measured is "s0 and s1", not "the fleet". Write `selector` as
  PromQL (`x{service_name="payment"}`, groupings as `sum by (code) (...)`); one that cannot be
  read leaves the scope `undetermined`.
- **Every entity the claim names is in its evidence.** The server reads which services, pods,
  jobs... each cited dataset covers (series labels, and the matchers of its own query, both
  sides of a ratio: `sum(a{svc="x"}) / sum(b{svc="x"})` covers x, `sum(a) / sum(b)` pools) and
  refuses a claim naming others (`claim_beyond_evidence`, with the datasets of the same metric
  that hold them): cite those too, or split the claim. Pooled evidence (`sum(rate(x[1m]))`)
  covers a service when the source holds one value of the label over its range: a dataset of
  the same metric by service, or `entities(kind="service")` (or `metric=`) over a window
  holding the range, listing exactly one; the result's `scope.message` names that witness. `scope_note` (why the claim reaches further) records it
  flagged `beyond_evidence` instead; use it rarely and repeat it in the report.
- **Time range and reference**: the window judged and what it was compared with ("against the 4
  previous hours", "the same hour on 7 previous days"). A verdict without its reference is not a
  claim.
- **Population**: requests (histogram observations) or per-step samples (scrape values);
  `show_marginal` and `fraction_over` say which. A share of requests is not a share of time.
- **Aggregation**: how members were combined (summed counts, per member). Never generalise from
  a member to the service, from one source to production, or from this window to "always".

Missing data narrows scope: `coverage`, `unknown_spans` and `silent_members` say where nothing was
measured. Say "no change detected in the 92% of the window that was measured", not "no change".

**Positive vs negative claims.** What was observed can be claimed outright ("s3 had no samples
10:20-10:45Z"). What is absent or complete ("nothing missing", "all members reported", "s3 left",
"it did not recur") holds only for the finite set examined: this window, these members. Time
series are never finite, so scope every negative claim to the interval and members analyzed, and
never extend it to the series, the service or the future. A series silent until the window end
is "no samples since 10:45Z", not "left"; a gap with samples on both sides is an observed
disconnect. When the negative question matters, name the wider check that would test it (for
example: is the set of `pod` values per bucket the same over the previous day). Principle 9.

**Absence of an entity or signal.** "There is no payment service", "no 5xx anywhere", "checkout
emits no metrics" are negative claims about a whole source. An empty query, an empty
`catalog_search` or one metric family without the entity is absence of evidence, not evidence of
absence: the entity may report through other metrics (span metrics) or under another label.
Before such a claim, run `entities(kind="service")` (or `entities(label=..., metric=...)`) and
cite it; then scope the claim to what it searched: "payment is not found under `service_name`,
`service`, `app`, `job` in 09:40-10:40Z". Without that check, say "not seen in the metrics
queried (list them)", never "does not exist". The `empty_result` and catalog `note` fields on
empty results say the same.

## Uncertainty policy (principle 4)

1. **Every statistic states its uncertainty**: an interval, `exact`, or
   `uncertainty_unknown: true`. Derive an interval before giving up (Wilson for shares, Poisson
   for counts, effective n for autocorrelated series, bucket edges for histograms, bootstrap).
2. **Unknown is unknown, not zero and not forbidden.** A value of unknown uncertainty may be
   cited; the finding then carries "uncertainty unknown" and the report says so. Never present
   it as exact, never drop it silently.
3. **Error aggregation is maximalist.** Errors propagate through every step; where propagation is
   impossible take the worst case. An interval that left out its inputs' error is a **lower
   bound** (`input_uncertainty_unknown`, `uncertainty_not_propagated`): say "at least this
   uncertain".
4. **Read the `finding_create` result.** Its `uncertainty` list names flags the server derived
   per evidence item; repeat them when reporting the finding.

Percentiles (principle 10): never average them across series or time, never cite one without its n
(n >= 10/(1-q): p95 ~200, p99 ~1000), and prefer the share of requests above a stated edge
(`fraction_over`, exact at bucket edges) as the statistic. A percentile statistic needs
`params.q` and `params.n`; the server refuses it without n, or with n below 10/(1-q).

## Sources of variation (principle 8)

Every variation gets a source label (wire values in backticks). Ops assign them: `source` on
items and on `evidence` statistics, `variation` lists in results.

| Label | Meaning | Action |
|---|---|---|
| `common_cause` | the system's inherent variability: control limits, seasonal band, fleet SPC band and spread, small-system envelope | do not chase points inside it; the lever is changing the system |
| `special_cause` | assignable: shifts, drift, out-of-limit signals, an unusual window or member, a changed role, a transient | investigate; this is what an incident finding is about |
| `measurement_system` | the instruments: gaps, partial or untrusted data, units, missing members, unknown input uncertainty, a systematic Little's law offset | fix or qualify the instrument before reading the process |
| `undetermined` | the data cannot tell them apart (a signal on partial data, a silent member, run rules on an out-of-control chart) | say so; name what would separate them |

Rules: report the label **with** the number, never instead of it. Pass `source` through to
`finding_create` unchanged; never relabel and never upgrade `undetermined` to `special_cause`.
A statistic cited without it gets the source its op gave it (flag source_derived in the
result's source_flags); a source that contradicts the op's label is refused
(source_relabelled), `undetermined` over an op's label is kept but flagged source_downgraded,
and a label on a statistic no op labelled is flagged source_unverified (counts as undetermined); one no op emitted, or a finding whose evidence attributes no variation
(a panel only), is flagged source_undetermined and lists `undetermined` in `sources`: for an
incident claim, cite the op statistic (`analyze`, `compare_seasonal`, `fleet`,
`binding_verdict`, `check_littles_law`) that labels it, not only its panel: a panel carries no
source. Op results end with a `cite` line saying where their evidence statistics are, and
finding_create returns `citable_statistics` (the labelled statistics behind cited panels,
special cause first) to cite as given.
Separate measurement-system findings from process findings: "the gauge misses instance i3" is
its own finding, not a caveat on a latency claim. A `common_cause` result supports "nothing
beyond normal variation was detected", which can rule a hypothesis out.

## Results are model outputs (principle 16)

Every p value, label and verdict comes from a model with assumptions; none is a fact. Word a
finding as "under model M …" or "consistent with …", and cite the model the op states (its
`method`, the `models` / `summary` fields): "under a Poisson model p = 2e-24; allowing
clustered errors (dispersion 13 from the judged steps) p = 0.015: undetermined". Where an op
reports an optimistic and a cautious model, the label rests on the cautious one: pass it
through (`undetermined` stays `undetermined`), quote the optimistic p only as context, and name
what would decide it (a longer baseline, history, a sibling signal).

## Always show the discrepancy (principle 12)

A verdict is context, never a replacement for the measured numbers. Lead with the measured
difference and its interval (L − λW, a ratio, a share before and after), then the verdict.
"Consistent", "usual" and "no change" mean "nothing detected at this precision against this
reference", not "healthy" or "correct". Every re-run with another range, reference or alpha is
another look; count the looks (principle 14).

## Hypotheses: for, against, ruled out (principle 13)

1. `hypothesis_create(statement, scope?)` as soon as an explanation is entertained, and record
   the competing ones too (load, saturation, one bad member, the daily peak, a dependency, the
   instruments). A statement names its subject (the service, resource or metric) and what
   would be true in the data: "payment `charge` calls fail and checkout errors follow", not "a
   fault in one service". `scope` says where it applies, in a finding scope's shape:
   `{selector, start, end, source?}` (its selector's services and metrics count as subjects);
   prose is stored as text, shown and not checked.
2. For each, find the observation that could **refute** it, and look for that first.
3. Attach evidence with `finding_create(..., hypotheses=[{"id": "h1", "stance": "for"},
   {"id": "h2", "stance": "against"}])`: one link per hypothesis the observation bears on (the
   rate that rose 3x backs "arrival surge" and refutes "slower at an unchanged rate").
   `hypothesis=<id>, stance=...` is the one-link form. A user reply that contradicts a
   hypothesis still needs a finding against it built from data.
4. Move the status with `hypothesis_update(hypothesis, status, note, reason?)`: `proposed`
   (default), `supported`, `refuted`, `inconclusive`. `supported` needs evidence for **and** the
   obvious alternatives refuted; timing alone (A moved before B) is `proposed`, not `supported`.
   The server refuses `supported` without a concrete subject in the statement, a standing
   `stance="for"` finding, and an alternative considered: another hypothesis `refuted` /
   `inconclusive`, or `alternatives_considered` (which, and how each was ruled out). It
   refuses `refuted` without a standing finding against it or a `reason` (what rules it out,
   citing findings; stored with the status), and `inconclusive` without a linked finding, a
   reason or a note. A note is not a reason: it does not link the finding it mentions.
   Refuted hypotheses stay visible: ruling out is a result.

Causation is never claimed from ordering or correlation. "Latency moved first, 09:56-10:02Z,
before the errors, 10:17-10:22Z" is a finding; "latency caused the errors" is a hypothesis.

## Gaps

`gap_create(missing_signal, needed_for, suggestion)` when a conclusion depends on a signal that
does not exist: a role with no metric, a percentile-only latency (needs a histogram), no
in-flight gauge for Little's law, no status label for errors, a missing member. `suggestion` is
`{name, type, labels?}` with `type` counter | gauge | histogram | summary. File it when the
missing signal changes what can be concluded; say in the report what stays unknown without it.

## Lessons: scoped methodology across sessions (principles 2, 16)

A lesson (`lesson_propose`, from `/telemetry-nerd:wrap`) is re-applied later without its
evidence in view, so it cites findings or panels and its scope (`{source, service?,
metric_family?, labels?}`) is never broader than one cited item covers: evidence about one
service makes a lesson about that service (else `lesson_beyond_evidence`: narrow the scope).
An approved lesson is a prior, not evidence. A finding that contradicts one:
`lesson_refute(lesson, evidence=[f…], reason)`. Established catalog values: `catalog_propose`.

## Recording a finding: checklist

- One claim per finding, in the scope's words, with numbers and intervals.
- Evidence: the op statistic(s) as returned, plus the panel that shows it, plus the annotation
  for an onset or event.
- Caveats from the op (`overdispersed`, `heavy_tails`, `settling`, gaps) in `caveats`.
- `scope.baseline_start` / `baseline_end` when the claim is relative to a reference.
- After the call: report the finding id, its `scope` status (when not `covered`), its
  `uncertainty` flags and its `sources` (including `undetermined`).

## Additional resources

- **`references/finding-anatomy.md`**: field-by-field `finding_create` examples, wording templates,
  and the anti-pattern list (unscoped, averaged percentiles, causal language).
- **`references/sources-of-variation.md`**: the per-op mapping of labels (analyze,
  compare_seasonal, fleet, binding_verdict, check_littles_law) and how to report each.

Related skills: `triage` (the incident flow that produces findings), `model-views` (verdicts),
`tier2-code` (declaring uncertainty for code outputs), `charting` (the panel that backs a claim).
