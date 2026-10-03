---
name: model-views
description: This skill should be used when a telemetry question concerns a service or resource as a whole rather than one metric: "is the service overloaded/healthy/degraded", "what changed first", "why are requests slow", "show RED / USE / golden signals", "is it saturated", "do concurrency, throughput and latency agree", "check Little's law", "bind these metrics", or when running binding_suggest, binding_accept, show_binding, binding_verdict or check_littles_law. Covers choosing and confirming a binding, reading the panel group, reporting verdicts honestly, and Little's law interpretation.
---

# Model views: bindings, USE / RED, Little's law

A **binding** says which metrics play which role of a model for one service or resource key:

| kind | roles | use for |
|---|---|---|
| `RED` | `rate`, `errors`, `duration` | request-driven services (HTTP, gRPC, queues consumers) |
| `USE` | `utilization`, `saturation`, `errors` | resources (CPU, disk, a connection or thread pool, a node) |
| `littles_law` | `arrival_rate`, `latency`, `concurrency` | does in-flight concurrency agree with throughput x mean latency |

Bindings are catalog claims with a basis, like relations. Three tools use them: `show_binding`
(one linked panel group), `binding_verdict` (per-signal health with evidence), `check_littles_law`
(consistency of the three Little's law signals). All three are tier-1: they carry validated
uncertainty, stated reference windows and `evidence` statistics. Do not rebuild them with
`run_code`.

## When to look for a binding

- "Is X overloaded / healthy / degraded?", "what changed first?", "why are requests slow?" about
  a named service or resource.
- A single metric panel raised the question but the answer depends on its siblings (latency
  without throughput, errors without requests, utilization without queueing).
- The user asks for RED / USE / golden signals, or concurrency vs throughput vs latency.

Skip it for one metric's own behaviour: `analyze`, `compare_seasonal`, `fleet` answer that.

Choose the model by what is being asked about: a request path is RED; a thing requests wait for
is USE; a service that exposes in-flight requests plus a latency histogram also earns a Little's
law check. A service can have a RED and a Little's law binding at once.

## Workflow

1. **Learn first.** `source_learn` once per source (the suggestions come from the catalog; nothing
   is queried). Check `catalog_relations(source, kind=...)` for an existing binding before
   proposing one.
2. **Propose.** `binding_suggest(source, kind=..., key=...)`. `key` narrows to a scope key
   (`http.server`, `node:cpu`) or names the service to bind for.
3. **Review the roles, never accept blind.** For each role read `detail`: `confidence`, `basis`
   (pack | naming | relation), `form`, the `expr` hint, `alternatives`, `ambiguous`. Check
   `unfilled` (a role nothing fills) and `join_on` (a label convention, not read from the source:
   verify against real series with `query`). Prefer histograms for latency: a suggestion should
   never pick a precomputed percentile while a histogram exists.
4. **Confirm.** `binding_accept(source, id, basis=...)`: `basis` states what was checked;
   `overrides={role: metric|null}` swaps a role for a listed alternative; `key` renames the
   entity. Use `catalog_bind(source, kind, key, roles, confidence, basis, join_on)` for a binding
   built by hand. An unfilled role raises a Gap exactly as in `catalog_bind`.
5. **Look.** `show_binding(source, kind=, key=, range="6h")` or `suggestion=<id>` to look before
   confirming. Pass `matchers={"service_name": "cart"}` to narrow every role; `error_matcher`
   when no status label is known (`status_code=~"5.."`).
6. **Judge.** `binding_verdict(group="pgN")` (the group from step 5), or `kind` + `key`.
7. **Report** per the rules below, and cite `evidence` in `finding_create`.

The binding `key` is the catalog key (`http.server`), not the suggestion id (`RED:otel_http`).
Only `suggestion=` takes the id. Details and pitfalls: `references/binding-workflow.md`.

## Reading the panel group

`show_binding` returns a panel group `pgN` and, per role, `{panel, metric, form, view, members,
notes}`, a `{gap, suggest}` card, or an `{error}`. One time range, step and selection are shared.

- `rate` / `arrival_rate`: counter rates. `errors` (RED): errors / requests with a Wilson 95%
  band, never a bare error count. `duration` / `latency`: the histogram as a heatmap or percentile
  bands where n suffices. `utilization`: natural 0..1 axis. `saturation`: the queue or pressure
  series, with the `bounded_by` limit line when one is known. More than 5 members: drawn as a
  fleet; follow with `fleet(dataset)` for the spread and outliers.
- Read the `notes`: label names in expr hints are conventions. A wrong label shows up as an empty
  role or a role `error`, not as a healthy signal.
- `concurrency` (Little's law binding): the in-flight gauge. The group does not judge Little's law:
  consistency of the three signals is `check_littles_law`'s job.
- A gap card names the instrumentation that would fill the role. Say what is missing and what
  it would cost not to know; record it with `gap_create` when it matters.
- Tell the user where to look (`highlight(object="pgN")`), but the verdict is not in the picture: run
  `binding_verdict`.

## Reporting a verdict honestly

`binding_verdict` compares each role against reference windows and returns `summary.text`, per
role `status`, `direction`, `pattern`, `onset`, and `evidence`. Report:

1. **The reference and the family.** Say what "before" was (`reference.label`, for example
   "4 previous windows" or "the same hour on 7 previous days") and the family-wise alpha over the
   roles (`family`): "at 5% family-wise over 3 signals". Never state a verdict without its
   reference. `reference="profile"` needs `operating_profile` first (it is refused otherwise);
   `auto` falls back to `previous`, which is weak for daily rhythms: say so.
2. **Per role, with the pattern.** `level` = the window differs but no onset is inside it (do not
   claim a start time). `shift` = one change point. `burst` / `blip` = an episode that ended
   inside the window (observed: data after it shows the return). `sustained` = still going at the
   end of the window. `insufficient` and `gap` are not "healthy".
3. **Ordering only as far as the intervals allow.** `summary.first` is null when onset intervals
   overlap: write "simultaneous within +-X", not "A caused B". "First" is first among roles that
   have an onset: a `level`-pattern role cannot be ordered and may have moved earlier. The
   family alpha controls false flags, not the ordering or the onset intervals (approximate, not
   a stated 95%). Order is timing of detected change, never causation.
4. **Latency is a share above a stated edge** (`threshold`: the bucket edge where the reference
   share above is nearest 5%), not a percentile and not a mean. Write "share of requests above
   250 ms went from 1.2% to 5.4%" with the intervals and n from `level` / `evidence`. A change
   entirely below the edge is invisible to it.
5. **`at_capacity` and `model_check` are not tests.** `at_capacity` (utilization at >= 90% of its
   bound for 3+ steps) qualifies a change; `model_check` (Little's law on the concurrency role)
   is reported, not counted in the family.
6. **Cite the evidence** objects as they are in `finding_create` (the statistic, its interval, the
   dataset), and carry their uncertainty flags: `input_uncertainty` / `input_uncertainty_unknown`
   mean the interval is a lower bound; say so (the finding carries the `input_uncertainty_unknown`
   or `uncertainty_not_propagated` flag; principle 4).
7. **Caveats** (`overdispersed`, `heavy_tails`, `noisier_than_reference`) go into the sentence,
   not into a footnote.
8. **The source of each change** (`roles.<role>.source`, also on the level and onset evidence and
   in `variation`): a changed role is a **special cause** (investigate), no change is **common
   cause** (inside the reference cycles' spread), and a change on data with measurement-system
   issues (partial fetch, failed spans, unknown input uncertainty) is **source undetermined**:
   say "it moved, but the data cannot tell a real change from a collection problem", and name
   the issue. Never upgrade undetermined to special cause.

Absence of a flag is "no change detected against this reference at this power", not "healthy".
Choose the range before looking at the verdict: every re-run with another range, reference or
alpha is another look that the family alpha does not cover; state how many looks were taken
(principle 14).
Detail, field meanings and wording templates: `references/verdicts.md`.

## Little's law check

`check_littles_law(source, binding=<key> | arrival_rate, latency, concurrency, by=[...], start,
end, window, warmup, latency_unit, arrivals)` (defaults: last 6h, `window="auto"` about range/12) tests L (the in-flight gauge, time-averaged) against
lambda x W (arrival rate times MEAN latency from histogram `_sum` / `_count`).

- Report the discrepancy FIRST, whatever the verdict (principle 12): `discrepancy` (L - lambda W and R - 1 =
  L / (lambda W) - 1, whole range and per window, with the measurement interval); `summary` is that
  sentence ready-made. Then the verdict (`consistent`, `L_high`, `L_low`,
  `inconsistent_in_windows`), `classification` (systematic offset, transient windows), then
  `warnings`, every entry of `assumptions` (ok / assumed / flagged) and `hints`.
- Name the source of each variation: **measurement system** (the measurement interval; a
  systematic offset = instrumentation / model mismatch: unmeasured queueing, a missing instance,
  units, subset/superset), **common cause** (small-system fluctuation +-X% per window at this
  traffic, and the windows' own spread: do not chase windows inside it), **special cause**
  (transient windows beyond both: a load peak leaving steady state — say "at a load peak"
  explicitly — a draining backlog, or a change confined to those windows; and load-peak windows
  `promoted` on independent evidence of leaving steady state: quote their `reason`). A load-peak
  window inside the envelope without such evidence is common cause: "not a signal by itself;
  watch if it repeats or grows".
- `L_high`: time in the system that the latency timer does not cover: queueing before the timer
  starts, leaked or stuck requests, latency on a subset, a gauge counting something broader.
  `L_low`: concurrency missing instances, a gauge missing bursts, latency on a superset.
  `L_high` can mean measured latency understates what the caller waits, but the check cannot tell
  queueing from the other causes without more evidence. `consistent` means no mismatch detected
  at this precision, not that the instruments are right: offsetting errors can cancel.
- No concurrency signal: the check cannot be done; say so with the suggested in-flight gauge and
  record a gap. Never derive L from lambda x W.
- Percentile-only latency is refused: a percentile is not a mean and Little's law is about the
  mean. Supply a histogram or summary with `_sum` and `_count`, or record a gap.
- Units: W is converted to seconds from `latency_unit`, the catalog, or the name suffix; otherwise
  assumed seconds and flagged (`latency_unit_assumed`). A ratio near 1000 or 0.001 is ms vs s.
- Draw with `show(<datasets.concurrency>, question, mark="littles")`.

Assumptions, what each violation implies, hints and worked reading: `references/littles-law.md`.

## Sources of variation

Every op that reports variation labels each finding (principle 8; spec §5.4): `source` on items and on
`evidence` statistics, and a `variation` list of `{source, finding}` in the result.

- **common cause**: the system's inherent variability (control limits, the seasonal band, the
  fleet's spread, behaviour groups as systemic structure, the small-system envelope). Do not
  chase points inside it; the lever is changing the system.
- **special cause**: assignable (SPC signals of a significant detector, shifts, drift, an unusual
  window or member, a changed role, a transient). Investigate.
- **measurement system**: the instruments (gaps, partial / untrusted data, the share of members
  missing per step, membership changes (n moves), units, unknown input uncertainty, a systematic
  Little's law offset). Fix or qualify them before reading the process. A membership change is
  normal lifecycle, not a fault: it is labelled because n moving changes fleet aggregates
  (principle 11).
- **source undetermined**: the data cannot tell them apart (run rules on an out-of-control chart
  without their own evidence, an outlier episode on partial buckets, a silent member, a
  user-excluded cycle). Say so; propose what would separate them.

Report the label with the number, never instead of it, and pass `source` through to
`finding_create` unchanged.

## Gaps and instrumentation

A role with no signal is a gap, not a zero (principles 4, 11). Report it as: what is missing, what cannot be
concluded without it, the instrumentation that fills it (the `suggest` text: a latency histogram
instead of a percentile gauge, an in-flight gauge, a saturation or queue-depth metric, a
status-labelled request counter). Create it with `gap_create` when the user is likely to want it.

## Additional resources

- **`references/binding-workflow.md`**: suggestion fields, ambiguity handling, join labels, errors.
- **`references/verdicts.md`**: reference schemes, patterns, onsets, ordering, wording templates.
- **`references/littles-law.md`**: assumptions, hints, units, windows, why means not percentiles.
- **`references/worked-examples.md`**: executed call sequences on seeded scenarios (error burst
  after a latency shift; unmeasured queueing). Every call in it is run by the test suite.
