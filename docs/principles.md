# Telemetry Nerd — Design principles

**This document is canonical.** Every spec, skill, guide and README states these principles by
pointing here; where any of them disagrees with this file, this file wins and the other source is
a bug. To change a principle, change it here first, then bring the dependent sources in line.
Numbers are stable: 1–9 come from the MVP design spec §1.2 (2026-09-30, extended 2026-10-03);
new principles are appended, never renumbered.

Each principle gives the rule, where it came from (a decision date where one is known) and where
it is enforced. Detailed policy lives in the linked spec sections; skills restate the rules as
operational instructions for Claude and cite the principle number.

Context: Telemetry Nerd is **not an "AI SRE"** (MVP spec §1.1). Claude is an analyst's assistant:
it proposes hypotheses, gathers evidence for and against, draws the graphs that make the data
legible, and suggests where to look next. Humans draw conclusions (principle 13).

---

## 1. Evidence first

Every claim links to evidence: a panel, a dataset statistic, an annotation, a fit or a catalog
claim. A claim without evidence cannot be recorded as a finding; a thread reply, a number read off
a chart by eye or a summary line without its statistic is not evidence.

- Decided: 2026-09-30 (MVP brainstorm).
- Enforced by: `FindingIn.evidence` (min 1 item) and the `EvidenceRef` union in
  `src/telemetry_nerd/workspace/models.py`; `finding_create` in `src/telemetry_nerd/mcp/server.py`;
  MVP spec §3.3 (evidence refs); `skills/evidence/SKILL.md` ("What counts as evidence").

## 2. Scoped claims

Every finding carries an explicit scope: source, selector, time range, step, aggregation and an
optional baseline. A claim is worded so that it is true only inside that scope; Claude never
generalises from a member to the service, from one source to production, or from a window to
"always". Enforced by schema, not by prompting.

- Decided: 2026-09-30.
- Enforced by: `Scope` in `src/telemetry_nerd/workspace/models.py` (required on `FindingIn`;
  `step` a positive duration, `auto` refused); MVP spec §3.3; `skills/evidence/SKILL.md` ("Scope every claim").

## 3. Correct over conventional

When industry-standard practice is wrong or misleading (averaging percentiles, peak-eroding
downsampling, autoscaled axes, spaghetti charts, dual y-axes, stacking non-additive series,
interpolating across gaps), we do the right thing and explain why. We model on scientific
computing (MATLAB, R, epidemiology, physics), not on dashboard tools. Conventional views exist only
on explicit user request and carry a caveat.

- Decided: 2026-09-30, user: "we should not do things which are wrong or misleading just because
  of industry standards."
- Enforced by: the chart validator `src/telemetry_nerd/charts/spec.py` (MVP spec §6.3; overrides
  need `override: {rule, reason}` and render a caveat); min/max-preserving level of detail (MVP
  spec §6.5); contextual y-range `src/telemetry_nerd/charts/yview.py` (MVP spec §6.2);
  `docs/telemetry-graphing-guide.md` §2; `skills/charting/references/marks.md` ("Refused or
  corrected conventional charts").

## 4. Every number carries its uncertainty and its aggregation

Intervals, counts, min/max envelopes and representation metadata travel with the data. A
statistic states an interval, `exact`, or `uncertainty_unknown`. Three rules (user decision
2026-10-02):

1. **Unknown is unknown, not zero and not forbidden.** A value of unknown uncertainty may be
   cited, but whatever cites it says "uncertainty unknown" visibly; it is never treated as exact
   and never refused for that reason alone.
2. **Error aggregation is maximalist.** Errors propagate through transforms; where propagation is
   impossible the worst case is taken. A later step never shrinks the error below what propagation
   implies (pooling independent inputs may legitimately narrow it; leaving their error out may
   not): an interval that left out its inputs' error is a lower bound.
3. **Derive before giving up.** Where a tool gives no uncertainty, derive one (bootstrap,
   effective n, bucket bounds, Wilson / Poisson, propagation) before falling back to "unknown".

- Decided: 2026-09-30 (principle); 2026-10-02 (the three rules; beads x2x, x2x.1, dl7, 4jk).
- Enforced by: MVP spec §5.3; `src/telemetry_nerd/core/uncertainty.py` (`mark_statistics`,
  `evidence_flags`); `src/telemetry_nerd/exchange/fmt.py` (`UNCERTAINTY_STATUS`,
  `output_uncertainty_status`); `core.wire.statistic`; `skills/evidence/SKILL.md` ("Uncertainty
  policy"); `skills/tier2-code/SKILL.md` ("Declaring uncertainty").

## 5. Every graph answers an explicit question

Panels cannot be created without a question; the question is shown in the panel header, and a
panel is evidence only for its own question.

- Decided: 2026-09-30.
- Enforced by: `show` (`question` required) in `src/telemetry_nerd/mcp/server.py`; panel anatomy
  (MVP spec §6.4); `skills/charting/SKILL.md`.

## 6. Bulk data never enters Claude's context

Claude works with handles and compact summaries; the server holds the data. Tool results are
small (IDs, summary, caveats, link); code prints aggregates, never rows.

- Decided: 2026-09-30.
- Enforced by: MVP spec §7.1 (results ≤ ~2 KB); `src/telemetry_nerd/core/summary.py`;
  `skills/tier2-code/SKILL.md` (procedure step 4).

## 7. The workspace is the single source of truth

Claude, the UI and tier-2 code all mutate the same object model through the same operation layer.
Datasets and nodes are immutable; deletes are soft, so evidence links never break.

- Decided: 2026-09-30.
- Enforced by: `src/telemetry_nerd/core/workspace_service.py`, `src/telemetry_nerd/workspace/`;
  MVP spec §2.3, §3.3 (invariants).

## 8. Every variation is labelled with its source

Variance, noise and error do not share one cause (SPC's central insight): **common cause** (the
system's inherent variability, including a small system's real per-window fluctuation; the
limits / bands are its envelope; act by changing the system, never chase a point inside it),
**special cause** (assignable: out-of-limit points, run-rule signals, shifts, transients, leaving
steady state; investigate) and the **measurement system** (instrumentation error and bias:
sampling, edges, unmeasured segments, units, missing members, partial or untrusted data). When the
data cannot tell them apart the label is **source undetermined**, never a guess, and never
upgraded to special cause. Labels add context; they never hide or replace the measured numbers.

The common-cause envelope that is drawn is a **reference**, so it must be stable (user decision
2026-10-03, fleet band: "stable to be usable; it need not be accurate"). The fleet band is an SPC
reference: per-step median ± 2σ/3σ with the robust σ the outlier tests use, pooled over
neighbouring steps, on the analysis scale. Points are named special causes only by the
family-wise tests (principle 14), never because they leave a zone: points beyond 3σ that no test
flagged are reported as a rate against what chance gives (0.27% if normal), never marked one by
one; only a step where the fleet widened faster than its pooled σ tracks is marked (a p-chart
against the window's own share, overdispersion-corrected, family-wise over the steps), as a
common-cause signal. A descriptive view (the per-step
quantiles across members) is a separate toggle and carries missing-member bounds (principles 9,
11), never a sampling interval. (Here, not in 14: the band's job is to be the envelope this
principle names; 14 governs the flags drawn against it.)

- Decided: 2026-10-03, user (beads 60j, gkk); fleet band 2026-10-03 (bead nq6).
- Enforced by: MVP spec §5.4 (vocabulary and per-op mapping); `src/telemetry_nerd/analysis/sources.py`;
  `source` on `StatisticRef` and `FindingIn.sources` in `src/telemetry_nerd/workspace/models.py`;
  `skills/evidence/SKILL.md` and `skills/evidence/references/sources-of-variation.md`; fleet
  band: `control_band` / `missing_bounds` in `src/telemetry_nerd/analysis/fleet.py`, fleet spec
  "Band: the SPC reference".

## 9. Positive claims vs negative claims

A positive claim says something *was observed* ("pod x had no samples 10:20–10:45", "p99 exceeded
2 s in 14 of 60 buckets"); one observation proves it. A negative or universal claim says something
is *absent* or *complete* ("nothing is missing", "every member reported", "pod x left", "it never
recurred"); it holds only over a finite set we have fully examined. Telemetry is never such a set:
a time series keeps growing, members join and leave, sources drop and backfill. So negative claims
are made only about the scoped past interval and the members actually analysed ("no gap in the 6 h
examined, for the 20 pods returned"), never about the series, the service or the future. Trailing
silence is "no samples since T", not "left" or "ended": a wider window may show it return; a gap
bounded by samples is a positively observed disconnect. Where a negative question matters, say
what wider check would test it (e.g. is set(pod) per bucket consistent over a longer window)
instead of asserting it.

- Decided: 2026-10-03, user.
- Enforced by: `skills/evidence/SKILL.md` ("Positive vs negative claims");
  `skills/triage/references/ruling-out.md` (notes); `bucket_state` keeps trailing silence `empty`
  (alive), `absent` only before the first sample (`src/telemetry_nerd/model/bucket_state.py`);
  fleet `stopped_reporting` lists `last_seen` (`src/telemetry_nerd/core/fleet_ops.py`).

## 10. Only mergeable statistics are aggregated; percentiles never are

Counts, sums, min and max merge; means and ratios merge only with their counts (ratio of sums,
never mean of ratios). Percentiles, medians, MAD / IQR and pre-computed quantiles are never
combined across series, members, cycles or time: aggregate the histogram (or raw data) first,
then take the statistic once. "p99 across the fleet" from per-pod p99s is meaningless, and so is
the median of member p99s. A percentile is never cited without its sample count (n ≥ 10/(1−q)).

- Decided: 2026-09-30 (as an instance of principle 3); user decision 2026-10-03 (fleet:
  "we can't aggregate percentiles").
- Enforced by: `src/telemetry_nerd/analysis/exprkind.py` (percentile aggregation refused);
  `src/telemetry_nerd/catalog/mergeability.py`; percentile refusals in `fleet`, `analyze`,
  `spectrum`, `filter`, `compare_seasonal`, `check_littles_law`; the percentile `params.q` / `params.n`
  check on `StatisticRef` in `src/telemetry_nerd/workspace/models.py`; `docs/telemetry-graphing-guide.md` §2 rules 4 and 14;
  `skills/charting/SKILL.md` ("Binding rules").

## 11. Missing data is information

"No info is itself info." Missing data is shown, never filled: no interpolation, no zero-fill, no
carrying forward; "no data" ≠ 0 ≠ "don't know", and an unknown (failed, unobservable) span is
neither present nor missing. Missingness is rarely random: a member that stops reporting before
the window end may be the sick one, so it is listed and labelled source undetermined: neither
missing data nor healthy, never dropped. Membership changes (members appearing, or stopping
reporting, inside the window) are normal lifecycle, not a fault; because n moves, they are a
measurement effect on fleet aggregates and labelled so (`measurement_system`), with text saying
it is normal lifecycle. A member that stopped and came back (a gap bounded by samples) is the
suspicious case; detecting leave-then-rejoin is not implemented yet (bead telemetry-nerd-ojo).

Work with the data there is (user decision 2026-10-03): claim checks run per series, over the
series the claim's selector matches. In a multi-series claim, silent, low-coverage or untrusted
members produce a warning and are labelled in the finding; the claim is blocked only when it
cannot be supported (more than half of the members, or none, usable). A single-series claim is
judged over the whole claim window. A selector whose label matchers match no evidence series,
or check none of them, blocks: the claimed series were not examined. The mechanics (silence
under 5 min at a window edge is lost scrapes, counted missing; longer edge silence is
membership, labelled) are in the missing-data spec §4.4.

- Decided: brainstorm 2026-10-01/02 (missing-data spec §1); user decisions 2026-10-03 (churn is
  normal; per-series claim checks, warn and label rather than block; "work with the data you
  have, be honest about quirks/suspicions and still provide value").
- Enforced by: `src/telemetry_nerd/model/bucket_state.py`, `src/telemetry_nerd/model/caveats.py`,
  `src/telemetry_nerd/core/summary.py` (`coverage`, `unknown_spans`, `silent_members`),
  `src/telemetry_nerd/core/coverage_check.py`, `src/telemetry_nerd/core/claim_scope.py`; `docs/superpowers/specs/2026-10-02-series-bundles-missing-data-design.md`;
  `docs/telemetry-graphing-guide.md` §2 rule 6 and §5a; `docs/data-source-quirks.md`.

## 12. Show the discrepancy; a verdict is context

When a check compares measurements (L vs λW, now vs reference, one member vs the fleet), the
measured difference with its interval comes first, whatever the verdict. "Consistent", "usual"
and "no change" mean "nothing detected at this precision against this reference", never "healthy"
or "correct". A persistent offset (systematic) is reported apart from deviations confined to some
windows (transient). At low counts fluctuation is a real property of a small system (common
cause): warn with its expected scale, never fold it into the measurement interval and never
suppress a finding because of it. When an optional input (a concurrency gauge) is missing, say so
plainly.

- Decided: 2026-10-03, user (bead 60j).
- Enforced by: `src/telemetry_nerd/analysis/littles.py`, `src/telemetry_nerd/core/littles_ops.py`
  (`summary` leads with the discrepancy); `docs/superpowers/specs/2026-10-02-littles-law-design.md`
  ("Discrepancy first"); `skills/evidence/SKILL.md` ("Always show the discrepancy");
  `skills/model-views/SKILL.md`.

## 13. Hypotheses, not verdicts

Claude proposes and tests explanations; humans conclude (finding verdicts are the user's). Every
entertained explanation is recorded with its competitors, and the observation that would refute
it is looked for first. Causation is never claimed from ordering or correlation: "A moved before
B" is a finding, "A caused B" is a hypothesis. `supported` needs evidence for and the obvious
alternatives refuted. Refuted hypotheses and rejected findings stay visible: ruling out is a
result.

- Decided: 2026-09-30 (MVP spec §1.1, §3.3 invariants).
- Enforced by: `Hypothesis`, `Finding.verdict` in `src/telemetry_nerd/workspace/models.py`;
  `hypothesis_create` / `hypothesis_update` / `finding_create(stance=...)`;
  `skills/evidence/SKILL.md` ("Hypotheses: for, against, ruled out");
  `skills/triage/references/ruling-out.md`.

## 14. Decide before looking; count the looks

The question, range and reference are chosen before the result is seen, and stated with it.
Reference windows and control limits come from a stated baseline or from history, never from
the window being judged. Each op's simultaneous tests (roles, members, windows, steps) share one
family-wise error budget, and every re-run with another range, reference or alpha is another look that the budget
does not cover: say how many looks were taken.

- Decided: 2026-10-02 (series-diagnostics, seasonal, fleet and binding-verdict designs).
- Enforced by: `src/telemetry_nerd/analysis/spc.py`, `src/telemetry_nerd/analysis/verdicts.py`,
  `src/telemetry_nerd/analysis/fleet.py`, `src/telemetry_nerd/analysis/littles.py` (family-wise
  alpha); `docs/superpowers/specs/2026-10-02-series-diagnostics-design.md`,
  `docs/superpowers/specs/2026-10-02-binding-verdicts-design.md`; `skills/evidence/SKILL.md`,
  `skills/model-views/SKILL.md`, `skills/triage/SKILL.md`.

## 15. Learned facts carry their origin; the user outranks

What the catalog knows about a metric (unit, type, bounds, relations, bindings) is a claim with
an origin, a confidence and a basis, never a bare fact. Precedence is user > Claude > measured
stats > repo context (code, docs, dashboards) > pack > metadata > naming rule; Claude's
confidence is capped at 0.9 (1.0 only for what the user verified), conflicts stay visible, and a `correlated` relation is observation, never truth.
A role with no signal is a gap, never a stand-in metric.

- Decided: 2026-09-30 (MVP spec §3.4, §4.3); 2026-10-01 (catalog-store, relations-bindings and
  claude-learning designs: claims with provenance, retractions, the cap); repo `context` origin
  with `catalog_context`.
- Enforced by: `ORIGIN_RANK` in `src/telemetry_nerd/catalog/models.py`; `_claude_gate` and
  `CLAUDE_MAX_CONFIDENCE` in `src/telemetry_nerd/core/workspace_service.py`;
  `src/telemetry_nerd/catalog/store.py`, `src/telemetry_nerd/catalog/relations.py`;
  `skills/metric-learning/SKILL.md`.
