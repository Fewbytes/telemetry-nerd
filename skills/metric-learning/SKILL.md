---
name: metric-learning
description: Use when learning what metrics in a connected source mean (unit, type, role, bounds, additivity, relations) so charts and analyses can rely on them, or when a chart's unit or semantics look wrong or unknown. Covers the catalog tools, how to gather evidence, and how much confidence each kind of evidence earns.
---

# Metric learning

The catalog holds what Telemetry Nerd knows about each metric, as **claims with an origin**. Cheap
tiers already ran at `source_learn`: declared metadata, naming conventions, and knowledge packs
(node_exporter, Kubernetes). Your job is the part they cannot do: interpret what is left, and fix
what they got wrong. Charts take units and semantics from the winning claim, so a wrong claim
becomes a wrong axis.

## Precedence

user > **claude (you)** > stats > pack > metadata > rule. You outrank packs, metadata and name
rules; you never outrank the user, and you do not try. If your write comes back
`effective: false`, someone outranks you: accept it.

## Procedure

1. `source_learn(source)` once per source. Read the caveats: `metadata_coverage:N%` means most
   metrics have no declared type or HELP, so naming rules did the work and may be wrong.
2. Pick what to learn. Hot metrics first (`catalog_search(source, needs_review=true)` lists the
   ones this workspace already queried first), then one family at a time using the `families`
   overview (`prefix="node_cpu"`). Do not sweep everything: learn what the investigation touches.
3. For each family, read the existing claims before proposing anything:
   `catalog_get(source, metric)` shows every claim, its origin and basis, and conflicts.
   A conflict is a finding about the source (a gauge named `_total`, a unit that disagrees with
   its name). Do not paper over it: resolve it with evidence, or leave it and say so.
4. Gather evidence proportional to the claim (below), then write the batch with `catalog_write`,
   one coherent family per call.
5. Report what you learned and what you could not establish.

## Evidence and confidence

`confidence` is at most 0.9; 1.0 is reserved for what the user verified. `basis` is required: one
line naming what you actually checked. A claim without a real basis is a guess; do not write it.

| Evidence | Confidence |
|---|---|
| Naming convention only (`_seconds`, `_total`) | 0.6-0.7 |
| Declared HELP text states it plainly | 0.75-0.85 |
| Source code, exporter docs, or the emitting library's documented behavior (cite file/URL) | 0.8-0.9 |
| Observed values consistent with the claim (range, monotonicity, resets) over a real window | 0.7-0.8, never more than documentation |
| "Probably" | do not write |

Rules that keep the catalog honest:

- **Do not claim a unit you cannot justify.** A `_seconds` metric may be a timestamp
  (`node_boot_time_seconds`), not a duration: claim `role=timestamp` and say so in the basis.
  No suffix is not "dimensionless".
- **Counters vs gauges.** `unknown` or missing declared type does not make something a gauge.
  Check monotonicity/resets before claiming `counter`, or cite documentation.
- **Bounds are physical, not typical.** `[0,1]` only if it cannot exceed 1 (CPU percent summed
  over cores can exceed 100). A tall peak is not a bound.
- **`bounded_by`** names a metric at the same label set that this one never exceeds
  (available <= total). Do not use it for usage-vs-limit across different label sets.
- **Additivity.** `additive` means summing across series (or over time, for increases) is
  meaningful. Ratios, percentages, averages and per-host gauges like load are `intensive`.
- **Prefer fixing over adding.** A correct unit on a metric the investigation uses is worth more
  than descriptions for fifty metrics nobody opened.
- Do not rewrite a pack's or the user's claims to match your taste. If you are sure a pack is
  wrong, write your claim with the evidence; it will win, and the conflict stays visible.

## Reading the result

`catalog_write` returns per-claim `accepted`/`rejected` with a reason. Fix rejected claims
(unknown metric: run `source_learn`; invalid value; missing basis). `effective: false` with
`outranked_by` means your claim is stored but another origin decides the value.
