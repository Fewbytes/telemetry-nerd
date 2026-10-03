# Finding anatomy, wording, anti-patterns

## A finding, field by field

```json
{
  "claim": "From about 10:00Z (09:56-10:02Z) the share of http.server requests slower than 0.25 s rose from 1.2% to 5.4% against the 4 previous hours",
  "scope": {
    "source": "default",
    "selector": "http_server_request_duration_seconds_bucket",
    "start": "now-1h", "end": "now", "step": "1m",
    "aggregation": "share of observations above 0.25 s, service_name s0+s1 summed",
    "baseline_start": "now-5h", "baseline_end": "now-1h"
  },
  "evidence": [
    {"kind": "statistic", "dataset": "d1", "name": "duration_odds_ratio_vs_reference",
     "value": 4.6, "interval": [3.7, 5.7], "method": "...", "source": "special_cause"},
    {"kind": "panel", "panel": "p3"},
    {"kind": "annotation", "annotation": "a1"}
  ],
  "caveats": ["overdispersed: intervals use effective n", "onset interval approximate"]
}
```

- `claim`: the measured change, its size with interval, its time, its reference. No adjectives
  the numbers do not carry ("severe", "massive").
- `scope.selector`: what was queried. `aggregation`: how members and time were combined.
- `evidence`: statistics copied from an op's `evidence` list unchanged (value, interval, method,
  params, source). Editing them breaks the link to the dataset.
- `caveats`: the op's caveats in words; the user reads them beside the claim.
- `hypothesis` + `stance`: only together; `stance` is `for` or `against`.
- `answers_panel`: the panel whose question the finding answers (its status becomes answered).

The result is `{finding, url, uncertainty?, sources?}`. Report the `uncertainty` flags
(uncertainty unknown, lower bound) and the `sources` with the finding id.

## Statistics without an op

When the number comes from tier-2 code, the code declares its uncertainty (`tn.put(...,
uncertainty=...)` or `exact=True`); the output dataset is then cited like an op statistic. A
value with no derivable interval is cited with `uncertainty_unknown: true`: allowed, flagged,
said aloud. Never invent an interval; never cite a fit parameter differently from how it was
stored.

## Wording templates

- Change: "<signal> (<scope>) <rose|fell> from <ref value> to <now value> (<interval>, n=<n>)
  against <reference>, from about <onset> (<onset interval>); source special cause."
- No change: "No change detected in <signal> against <reference> at <alpha> (ratio <x>,
  interval <lo>-<hi>): within normal variation (common cause)." Not "healthy".
- Undetermined: "<signal> moved, but <issue: partial data / silent member / unknown input
  uncertainty> means the data cannot tell a real change from a collection problem."
- Measurement system: "<instrument> <issue> (<number>): read <dependent claims> with this in
  mind." Its own finding.
- Ordering: "<A> moved before <B> (<A interval> vs <B interval>, non-overlapping): timing, not
  cause." Overlapping intervals: "simultaneous within +-<X>".
- Lower bound: "<interval> is a lower bound: <input> has unknown uncertainty."

## Anti-patterns

| Do not | Instead |
|---|---|
| "Latency is up" | "The share of requests above 250 ms in s0 rose from 1.2% to 5.4% (...) against the 4 previous hours" |
| average or max p99 across pods or hours | aggregate the histogram, then take the share or quantile once |
| cite a p99 without n | give n per bucket; say where the quantile is not meaningful |
| "no anomalies, healthy" | "nothing detected against <reference> at <alpha>; <coverage>" |
| "A caused B" from ordering | a hypothesis, `proposed`, with the timing finding attached `for` |
| chase a point inside the control limits | common cause: report the envelope, not the point |
| drop an undetermined label because it is inconvenient | say what would separate the sources |
| relabel an op's `source` | pass it through unchanged |
| treat a missing role as zero errors | a gap: what is missing and what cannot be concluded |
| delete or overwrite a refuted hypothesis | `hypothesis_update(hypothesis, status, note)` to `refuted`; it stays visible |
| restate numbers without the statistic | cite the op's `evidence` item |
