---
title: "Rationale"
description: "Why Telemetry Nerd is an investigation workspace rather than a dashboard, and the design principles that keep its answers honest."
---

Most of the time, a graph of a metric is good enough. You glance at a dashboard, the line is
flat, you move on. Telemetry Nerd is built for the other times: the incident where three
graphs disagree, the capacity review where someone has to say how much headroom is left, the
moment a line looks wrong and nobody can say why. Those moments call for an investigation, and
an investigation needs different tools from a dashboard.

This page explains what Telemetry Nerd is for, how it differs from the graphing systems you
already use, and the principles that shape every part of it.

## What it is, and what it isn't

Telemetry Nerd is an analysis workspace that you share with Claude. Claude queries your
Prometheus-compatible source, draws graphs into a browser workspace you both see, and writes down
what it thinks is going on as hypotheses and findings. Every finding points at the evidence behind
it. You look at the same graphs, ask about any part of them, and decide which hypotheses hold.

It is deliberately **not** three things:

- **Not an "AI SRE".** It does not take actions on your behalf. It reads metrics and never changes
  your infrastructure.
- **Not a dashboarding tool.** There are no wallboards or KPI grids. Panels appear in the order an
  investigation produces them, and each one exists to answer a single question.
- **Not an alerting system.** It does not watch your systems. You bring it a question.

The division of labour is fixed. Claude proposes explanations, gathers evidence for and against
them, draws the graphs that make the data legible and suggests where to look next. Humans draw the
conclusions: the verdict on a finding is always yours.

## A different job from Grafana and Kibana

Grafana, Kibana and similar tools are very good at what they are designed for: putting many
signals in front of an operator at a glance, and raising an alert when a threshold is crossed. They
render whatever query you give them and leave the interpretation to you, which is the right
trade-off for a wall of graphs you watch every day.

Investigation is a different job. The question is no longer "is anything red?" but "what is
actually happening, how sure are we, and what would prove us wrong?" A tool for that job needs
things a rendering layer does not have: knowledge of what each metric means, the ability to run
real analyses rather than only plot series, an honest account of what the data cannot tell you,
and a way to record conclusions so they can be checked. That matters twice over once an AI agent
is reading the graphs, because an agent that reads a misleading chart inherits its mistakes and
then reports them confidently.

### It knows what your metrics mean

To a graphing tool, a metric is a name and a series of numbers. To draw or analyse it correctly
you need more than that. A counter, a gauge and a histogram need different treatment, and so do a
percentage, a byte count and a ratio.

Telemetry Nerd keeps a catalog of what each metric is: its type, its unit, its natural bounds,
whether it can be summed across series or over time, its role (utilization, latency, errors and
so on), and how it relates to other metrics as parts, limits or derivations. Those facts come
from several places: the source's own metadata, naming conventions, curated knowledge packs for
common exporters such as node_exporter and Kubernetes, the measured behaviour of the samples, the
code and dashboards in your repository, and Claude working through the metrics with you.

None of these facts is stored as bare truth. Each one records its origin, its confidence and its
basis, and a fixed order of precedence decides between them: your word beats Claude's, Claude's
beats measured statistics, and so on down to naming rules. Claude can never claim more than 0.9
confidence on its own; only what you have verified reaches 1.0. When a metric's samples
contradict what the catalog says about it (a "counter" that occasionally goes down), that becomes
a finding you can see, not a silent overwrite.

This context changes what you see:

- Counters are drawn as rates, never as an ever-growing running total.
- Bounded metrics get their natural axis, so a ratio sits on 0 to 1 and a 3% wiggle does not fill
  the screen like a crisis.
- Physical limits are drawn alongside usage: available memory against total memory, available
  replicas against all replicas.
- A 30-day operating profile gives each metric a reference range and a normal band for the current
  hour of the week, so an unusual value stands out without anyone having set a threshold.
- Related metrics are grouped into models of a service (RED, USE, Little's law). When a model is
  missing a signal, such as a concurrency gauge, that is recorded as an open gap in the
  investigation rather than papered over with a stand-in.

### It runs the analysis, not just the query

A dashboard shows you a series and leaves the arithmetic to you. In practice, most of that
arithmetic never gets done: nobody computes a periodogram during an incident, or checks whether
a "spike" is outside what last month's Tuesdays looked like. Telemetry Nerd treats these analyses
as first-class operations that Claude can run and you can inspect:

- **Seasonal comparison.** "Is this unusual?" is answered against the spread of the same hour on
  previous days and weeks, or against the operating profile, not against one noisy reference
  window.
- **Periodicity.** Periodograms and spectrograms find cycles in a series, and each peak carries a
  significance level.
- **Fleet analysis.** Many series of one metric (a hundred pods, say) are analysed as a group: the
  spread drawn as a band, the median on top, and only the outliers drawn as labelled lines. A
  panel reads "44 pods, 3 outliers, 2 silent" instead of showing a hundred overlapping lines.
- **Distributions.** Latency histograms are shown as heatmaps, ECDFs and CCDFs, and you can ask
  exactly what fraction of requests crossed a threshold, with bounds at the histogram's bucket
  edges.
- **Model checks.** A service bound to Little's law can be checked directly: measured concurrency
  against throughput times mean latency. The discrepancy is always shown with its interval, and a
  persistent offset (for example, time the latency timer does not cover) is reported separately
  from deviations confined to a few windows.
- **Verdicts across a model.** For a RED or USE binding, Telemetry Nerd can say which signals moved
  against a stated reference, when, and which moved first, while holding the false-alarm rate
  across all roles at once.

When a question goes beyond the built-in operations, Claude can write analysis code that runs
next to the data. Either way, the bulk data stays on the server: Claude works with handles and
compact summaries, never with raw rows in its context.

### Missing data and uncertainty are part of the answer

Two habits of conventional graphing do quiet damage in an investigation. Gaps in the data get
bridged with a straight line, so a period with no samples looks like a calm system. And numbers
are shown without any indication of how precise they are, so a difference that is pure noise
looks the same as a real shift.

Telemetry Nerd treats both as information. Lines break at gaps instead of bridging them. Empty,
partial and untrusted buckets are marked under the graph, each state drawn differently, and named
in a caveat. "No data", zero and "don't know" are three different things and never look alike.
A member that stops reporting partway through a window is listed rather than dropped, because the
member that goes quiet is often the sick one, and a band drawn over the survivors looks healthy
precisely because the sick ones dropped out.

Every statistic states an interval, says that it is exact, or says plainly that its uncertainty is
unknown. Errors propagate through transforms, and where they cannot be propagated the worst case
is taken. A later step is never allowed to quietly shrink the error.

### The output is a claim, not a picture

A dashboard's output is a chart, and everything after that happens in someone's head or in a
chat thread. Telemetry Nerd's output is a set of typed objects in the workspace: hypotheses, with
their competitors and their status; findings, each with an explicit scope and at least one piece
of attached evidence; open gaps and annotations. Every graph carries the question it was drawn to
answer, shown in its header.

That makes an investigation checkable. You can open any finding and see exactly which panel,
statistic or fit supports it, over which series and which time range. A week later, someone who
was not there can follow the same trail.

### Correct over conventional

Many "industry standard" ways of showing telemetry are wrong in ways that matter during an
investigation. Telemetry Nerd models itself on scientific computing (R, MATLAB, epidemiology,
physics) rather than on dashboard conventions, and refuses the misleading forms unless you ask for
them explicitly, in which case the panel carries a caveat saying why.

| Conventional practice | What goes wrong | What Telemetry Nerd does |
|---|---|---|
| Downsample by averaging | A 5-minute spike disappears once it is averaged into 30-minute points | Every rolled-up line carries its min/max envelope, so peaks keep their real height |
| Average percentiles across hosts or time | A p99 averaged across hosts is nobody's p99 | Percentiles are only computed from merged histograms or raw data, never combined |
| Draw lines across gaps | Missing data looks like a steady system | Lines break at gaps; missing and untrusted buckets are marked and named |
| Connect bucket values with straight lines | Implies continuity the data does not have | Lines are stepped, because each value stands for its whole bucket |
| Autoscale the y-axis to the data | Small wiggles look like crises | A contextual range from the metric's reference and natural bounds; any data zoom is badged |
| Dual y-axes, stacked gauges or ratios | Arbitrary visual correlation; stacks that add non-additive quantities | Small multiples on a shared time axis; stacking only for additive series |
| Plot every series in a group | Spaghetti: no individual line is readable past a handful | A fleet band with the outliers drawn and labelled |

The percentile rule is not a matter of taste. In one worked example from Heinrich Hartmann's
SREcon19 talk "Latency SLOs Done Right", averaging 24 hourly p90 values gives 60.3 ms, while the
true p90 of the 811k merged requests is 35.8 ms: a 68.5% error, because quiet hours with a few
dozen requests count as much as peak hours with tens of thousands. The question that does
aggregate correctly is "what fraction of requests were faster than X?", and that is the one
Telemetry Nerd answers.

## The design principles

Everything above follows from sixteen principles, kept in one canonical document in the
repository (`docs/principles.md`). Each one is backed by code, schema validation or a skill that
states the rule as an operational instruction, not just by asking the agent to behave. Four of
them shape the experience most directly.

### Evidence first, and every claim scoped

A finding cannot be recorded without evidence: a panel, a dataset statistic, an annotation, a fit
or a catalog claim. A sentence in a chat thread is not evidence, and neither is a number read off
a chart by eye. Every finding also carries an explicit scope: the source, the series selector, the
time range, the step, the aggregation and, optionally, the baseline. The claim is worded so that it
is true only inside that scope.

This matters because the most common error in an investigation is not a wrong number but a
generalisation: one pod becomes "the service", one region becomes "production", one bad hour
becomes "always". Telemetry Nerd checks that the entities a claim names are covered by its
evidence, and marks a claim that reaches beyond its evidence as such instead of letting it pass.
The same rule applies to lessons carried between sessions: a lesson cannot be broader than the
evidence it cites.

### Missing data is information

"No info is itself info." Missing data is shown, never filled: no interpolation, no zero-fill, no
carrying the last value forward. A related principle governs what can be said about absence. A
positive claim ("pod x had no samples from 10:20 to 10:45") is proved by one observation. A
negative claim ("nothing is missing", "pod x left", "it never recurred") holds only over a
finite set you have fully examined, and telemetry is never such a set: series keep growing,
members join and leave, sources drop data and backfill it. So negative claims are made only about
the scoped past window and the members actually analysed. Trailing silence is reported as "no
samples since T", never as "left", because a wider window may show it come back.

In practice this means Telemetry Nerd works with the data that exists and is honest about its
quirks. A claim over many series that includes a few silent members gets a warning and a label;
it is blocked only when it cannot be supported at all.

### Every variation is labelled with its source

Borrowed from statistical process control: not all variation has the same cause, and treating it
as if it did leads to bad decisions. **Common cause** variation is the system's inherent
variability, the envelope it lives in; chasing individual points inside it is wasted effort, and
the fix, if one is needed, is to change the system. **Special cause** variation is assignable:
shifts, transients, points flagged by the statistical tests. That is what to investigate. The
**measurement system** is the third source: sampling, unit errors, missing members, partial or
untrusted data.

When the data cannot tell these apart, the label is "source undetermined", never a guess and
never quietly upgraded to special cause. Points are named special causes only by family-wise
tests, not because they happen to cross a band: with a hundred members, a quarter of the time
steps would show some point beyond three sigma purely by chance. Paired with this is a reporting
rule: when a check compares two measurements, the measured difference and its interval come first,
whatever the verdict. "Consistent" means "nothing detected at this precision against this
reference", never "healthy".

### Hypotheses, not verdicts

Claude proposes and tests explanations; you conclude. Every explanation Claude entertains is
recorded alongside its competitors, and the observation that would refute it is looked for first.
Ordering and correlation never become causation: "A moved before B" is a finding, "A caused B" is
a hypothesis. Claude can only mark a hypothesis supported when it has a standing finding for it
and the obvious alternatives have been refuted or found inconclusive. Refuted hypotheses and
rejected findings stay visible, because ruling something out is a result.

The companion principle is that results are model outputs, not facts. Every test, label and
verdict rests on assumptions (Poisson counts, independent steps, a stationary baseline), and the
operation states them alongside the result. Where a more cautious model is plausible, for example
one that allows for clustered errors, both results are shown, and a label is assigned only if it
holds under the cautious one. Otherwise it is "undetermined", with the optimistic result kept as
context.

### The rest, briefly

The remaining principles are less visible but just as load-bearing:

- **Correct over conventional (3).** The table above. Conventional views exist only on request and
  carry a caveat.
- **Every number carries its uncertainty and its aggregation (4).** Unknown uncertainty is shown as
  unknown, never treated as zero. Errors are aggregated pessimistically. Where a tool gives no
  uncertainty, one is derived (bootstrap, effective sample size, bucket bounds, Wilson or Poisson
  intervals) before falling back to "unknown".
- **Every graph answers an explicit question (5).** A panel cannot be created without one, and a
  panel is evidence only for its own question.
- **Bulk data never enters Claude's context (6).** Claude works with handles and compact
  summaries. This keeps the agent fast and stops it from "reading" thousands of rows it cannot
  reason about reliably.
- **The workspace is the single source of truth (7).** Claude, the UI and analysis code all change
  the same object model through the same operations. Datasets are immutable and deletes are soft,
  so evidence links never break.
- **Only mergeable statistics are aggregated (10).** Counts, sums, minima and maxima merge; means
  and ratios merge only with their counts. Percentiles, medians and pre-computed quantiles never
  do, and a percentile is never cited without the sample count that supports it.
- **Decide before looking; count the looks (14).** The question, range and reference are chosen
  before the result is seen. Reference windows come from a stated baseline or history, never from
  the window being judged. Simultaneous tests share one error budget, and re-running with a
  different range is another look that has to be reported.
- **Learned facts carry their origin; the user outranks (15).** Everything the catalog knows is a
  claim with an origin and a confidence, and a role with no signal is a gap, never a stand-in.

## Where this leaves your dashboards

Telemetry Nerd does not replace your dashboards or your alerts. It connects to the same
Prometheus-compatible sources, including Grafana datasource proxies, and it can read the code,
docs and dashboard definitions in your repository as context for what your metrics mean. Keep using them for what they are good
at. When a graph looks wrong and you need to know why, how sure you can be, and what would change
your mind, that is the job Telemetry Nerd was built for.
