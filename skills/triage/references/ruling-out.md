# Ruling out: common hypotheses and their tests

Each row: the hypothesis, the observation that would refute it, the tool, and how to cite the
result. Record the hypothesis first (`hypothesis_create`), then attach the result as a finding
with `stance` for or against, then `hypothesis_update`.

| Hypothesis | Refuted when | Tool | Cite |
|---|---|---|---|
| Traffic increase (load) drove it | request rate unchanged against the reference while the symptom moved | `binding_verdict` (rate role `no_change`, common cause) or `analyze(dataset, baseline="previous")` on the rate | the rate role's `evidence` statistic, `stance="against"` |
| It is the normal peak of this hour or weekday | now is outside the seasonal band | `compare_seasonal(dataset)` (latency: from the histogram) | `share_over.evidence` or `ratio.evidence` per series |
| One bad instance | the deviation is fleet-wide; `fleet` names no outlier, or every member is unusual | `fleet(dataset)` (5 or more members), per-member `compare_seasonal` or `fraction_over(..., by_series=true)` | outlier `evidence`, or each member's statistic |
| Saturation of a resource | USE utilization not `at_capacity`, saturation role unchanged | `show_binding` / `binding_verdict` on a USE binding | the role statistics |
| Hidden queueing before the timer | `check_littles_law` consistent (no `L_high` offset) | `check_littles_law(binding=...)` | `littles_law_discrepancy` statistic; an L_high result is evidence for |
| Fast errors flatter latency / slow errors hide in it | success and failure latency move alike | `split_outcome(dataset)` | the two panels plus `fraction_over` on each |
| A periodic job (cron, GC, backup) | no significant period near the symptom's spacing | `spectrum(dataset)` | significant peak `evidence`, or the stated `limits` |
| A gradual leak or drift, not a step | `analyze` reports a shift with an onset, no drift | `analyze(dataset)` | trend / shift `evidence` |
| The instruments, not the service | coverage complete, no partial or untrusted data, units checked, no silent members | `query` summary `coverage`, `unknown_spans`, `silent_members`; `catalog_get` for units | a measurement-system finding of its own when it is the instrument |
| A deploy or config change at the onset | no event inside the onset interval | an `annotate` event from the user's deploy log; compare it with the onset interval | the annotation, with the onset statistic |

Notes: "not refuted" is not "supported", timing is not cause, and an `undetermined` result is
neither for nor against. Status rules and wording: the `evidence` skill.
