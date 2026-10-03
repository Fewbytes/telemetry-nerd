---
name: evidence
description: This skill should be used when recording or stating any conclusion about telemetry in Telemetry Nerd: before calling finding_create, hypothesis_create, hypothesis_update or gap_create, when asked to "record a finding", "write up what we found", "is this evidence enough", "summarise the investigation", "rule this out", "what is the uncertainty", "is this a real change or noise", or when deciding whether a number, a verdict or a chart supports a claim. Covers what counts as evidence, scoping, the uncertainty policy (unknown is citable but flagged, maximalist propagation), sources of variation (common cause, special cause, measurement system, undetermined), hypotheses with evidence for and against, and when to file a gap.
---

# Evidence discipline

A claim in Telemetry Nerd is a **finding**: a scoped statement backed by evidence objects the
user can open. Hypotheses are explanations under test; gaps are signals that would be needed but
do not exist. The rules below are binding: correct over conventional, never what a dashboard
habit suggests.

## What counts as evidence

| Evidence item | Shape in `finding_create` | Counts when |
|---|---|---|
| statistic | `{kind: "statistic", dataset, name, value, method, interval: [lo, hi] \| exact: true \| uncertainty_unknown: true, source?}` | it came from an op's `evidence` (pass it **as returned**) or a code output that declared its uncertainty |
| panel | `{kind: "panel", panel: "p3"}` | the panel answers the question the claim is about |
| annotation | `{kind: "annotation", annotation: "a2"}` | it marks the event or region the claim cites (an onset, a deploy) |

Not evidence: a thread reply, a number read off a chart by eye, a summary line without its
statistic, a percentile without its sample count, an averaged percentile, a fit tagged
`weak_fit`. Prefer the op's own `evidence` statistic over restating its numbers: it carries the
method, the interval, the dataset and the source label.

## Scope every claim

`scope` is required: `{source, selector, start, end, step, aggregation, baseline_start?,
baseline_end?}`. Write the claim so it is true **only** inside that scope:

- **Service and selector**: the metric and label filter the numbers came from, not the service
  name in general. Two members measured is "s0 and s1", not "the fleet".
- **Time range and reference**: the window judged and what it was compared with ("against the 4
  previous hours", "the same hour on 7 previous days"). A verdict without its reference is not a
  claim.
- **Population**: requests (histogram observations) or per-step samples (scrape values);
  `show_marginal` and `fraction_over` say which. A share of requests is not a share of time.
- **Aggregation**: how members were combined (summed counts, per member). Never generalise from
  a member to the service, from one source to production, or from this window to "always".

Missing data narrows scope: `coverage`, `unknown_spans` and `silent_members` say where nothing was
measured. Say "no change detected in the 92% of the window that was measured", not "no change".

## Uncertainty policy

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

Percentiles: never average them across series or time, never cite one without its n
(n >= 10/(1-q): p95 ~200, p99 ~1000), and prefer the share of requests above a stated edge
(`fraction_over`, exact at bucket edges) as the statistic.

## Sources of variation

Every variation gets a source label (wire values in backticks). Ops assign them: `source` on
items and on `evidence` statistics, `variation` lists in results.

| Label | Meaning | Action |
|---|---|---|
| `common_cause` | the system's inherent variability: control limits, seasonal band, fleet spread, small-system envelope | do not chase points inside it; the lever is changing the system |
| `special_cause` | assignable: shifts, drift, out-of-limit signals, an unusual window or member, a changed role, a transient | investigate; this is what an incident finding is about |
| `measurement_system` | the instruments: gaps, partial or untrusted data, units, missing members, unknown input uncertainty, a systematic Little's law offset | fix or qualify the instrument before reading the process |
| `undetermined` | the data cannot tell them apart (a signal on partial data, a silent member, run rules on an out-of-control chart) | say so; name what would separate them |

Rules: report the label **with** the number, never instead of it. Pass `source` through to
`finding_create` unchanged; never relabel and never upgrade `undetermined` to `special_cause`.
Separate measurement-system findings from process findings: "the gauge misses instance i3" is
its own finding, not a caveat on a latency claim. A `common_cause` result supports "nothing
beyond normal variation was detected", which can rule a hypothesis out.

## Always show the discrepancy

A verdict is context, never a replacement for the measured numbers. Lead with the measured
difference and its interval (L − λW, a ratio, a share before and after), then the verdict.
"Consistent", "usual" and "no change" mean "nothing detected at this precision against this
reference", not "healthy" or "correct". Every re-run with another range, reference or alpha is
another look; count the looks.

## Hypotheses: for, against, ruled out

1. `hypothesis_create(statement)` as soon as an explanation is entertained, and record the
   competing ones too (load, saturation, one bad member, the daily peak, a dependency, the
   instruments). A statement names what would be true in the data.
2. For each, find the observation that could **refute** it, and look for that first.
3. Attach evidence with `finding_create(..., hypothesis=<id>, stance="for" | "against")`. A user
   reply that contradicts a hypothesis still needs a finding with `stance="against"` built from
   data.
4. Move the status with `hypothesis_update(hypothesis, status, note)`: `proposed` (default),
   `supported`, `refuted`, `inconclusive`. `supported` needs evidence for **and** the obvious
   alternatives refuted; timing alone (A moved before B) is `proposed`, not `supported`.
   Refuted hypotheses stay visible: ruling out is a result.

Causation is never claimed from ordering or correlation. "Latency moved first, 09:56-10:02Z,
before the errors, 10:17-10:22Z" is a finding; "latency caused the errors" is a hypothesis.

## Gaps

`gap_create(missing_signal, needed_for, suggestion)` when a conclusion depends on a signal that
does not exist: a role with no metric, a percentile-only latency (needs a histogram), no
in-flight gauge for Little's law, no status label for errors, a missing member. `suggestion` is
`{name, type, labels?}` with `type` counter | gauge | histogram | summary. File it when the
missing signal changes what can be concluded; say in the report what stays unknown without it.

## Recording a finding: checklist

- One claim per finding, in the scope's words, with numbers and intervals.
- Evidence: the op statistic(s) as returned, plus the panel that shows it, plus the annotation
  for an onset or event.
- Caveats from the op (`overdispersed`, `heavy_tails`, `settling`, gaps) in `caveats`.
- `scope.baseline_start` / `baseline_end` when the claim is relative to a reference.
- After the call: report the finding id, its `uncertainty` flags and its `sources`.

## Additional resources

- **`references/finding-anatomy.md`**: field-by-field `finding_create` examples, wording templates,
  and the anti-pattern list (unscoped, averaged percentiles, causal language).
- **`references/sources-of-variation.md`**: the per-op mapping of labels (analyze,
  compare_seasonal, fleet, binding_verdict, check_littles_law) and how to report each.

Related skills: `triage` (the incident flow that produces findings), `model-views` (verdicts),
`tier2-code` (declaring uncertainty for code outputs), `charting` (the panel that backs a claim).
