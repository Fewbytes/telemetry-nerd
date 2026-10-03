# Worked triage example

Each `json` block marked `<!-- call: ... -->` is one MCP tool call, run in order by
`tests/unit/test_plugin_skills.py` against a seeded scenario through the real MCP path; the
statements under each call are asserted there. A string argument starting with `$` stands for a
value from an earlier call's result: `"$verdict.roles.rate.evidence.0"` is the first `evidence`
item of the `rate` role in the call marked `id=verdict`. Pass such values on exactly as returned.
Timestamps are of the fixture clock (now = 10:40Z).

Fixture: an OTel HTTP server (`http_server_requests_total`, histogram
`http_server_request_duration_seconds`) with two members (`service_name` s0, s1). Unknown to the
investigator: the latency median rose 1.6x at 10:00Z and the 5xx share rose 10x from 10:20Z to
10:27Z. Request rate is unchanged.

## 1. Symptom and window

The user says: "requests got slow in the last hour, and we saw some 500s". Symptom: latency
(and errors) of the HTTP server. Window: the last hour, judged against the 4 preceding hours.
One look is planned for the verdict; any re-run with another range is counted.

## 2. Orient

<!-- call: scenario=triage id=learn -->
```json
{"tool": "source_learn", "args": {"source": "default"}}
```

<!-- call: scenario=triage id=existing -->
```json
{"tool": "catalog_relations", "args": {"source": "default", "kind": "RED"}}
```

Read: no RED binding exists yet (`bindings` is empty), so propose one.

## 3. Golden signals (model-views workflow)

<!-- call: scenario=triage id=suggest -->
```json
{"tool": "binding_suggest", "args": {"source": "default", "kind": "RED"}}
```

Read: `RED:otel_http`, key `http.server`, nothing unfilled, `duration` is the histogram.

<!-- call: scenario=triage id=accept -->
```json
{"tool": "binding_accept", "args": {"source": "default", "id": "RED:otel_http", "basis": "service_name on all three metrics; the 5xx split of the request counter and the classic latency histogram checked with query"}}
```

<!-- call: scenario=triage id=group -->
```json
{"tool": "show_binding", "args": {"source": "default", "kind": "RED", "key": "http.server", "range": "1h", "step": "1m"}}
```

Read: panel group `pg1`; `duration` is drawn as a heatmap (`p3`), `errors` as an error ratio.

<!-- call: scenario=triage id=verdict -->
```json
{"tool": "binding_verdict", "args": {"source": "default", "group": "$group.group", "reference": "previous"}}
```

Read: against 4 previous windows at 5% family-wise over 3 signals, `duration` moved first
(`shift`, onset 10:00Z, interval 09:56-10:02Z, share above 0.25 s 1.2% -> 5.4%, source special
cause); `errors` next (`burst`, onset 10:20Z, 10:17-10:22Z, special cause); `rate`: no change
(common cause, ratio about 0.99 with an interval spanning 1). The onset intervals do not overlap,
so the order is claimed as timing, not as cause.

## 4. Blast radius and seasonal baseline

Which services carry it, and is it just the usual load of this hour? Fetch the histogram per
member (histograms first) and compare it with the same hour on the 7 previous days:

<!-- call: scenario=triage id=dist -->
```json
{"tool": "query_distribution", "args": {"selector": "http_server_request_duration_seconds_bucket", "by": ["service_name"], "start": "now-1h", "end": "now", "step": "1m"}}
```

<!-- call: scenario=triage id=seasonal -->
```json
{"tool": "compare_seasonal", "args": {"dataset": "$dist.dataset", "cycles": ["1d"]}}
```

Read: both members are `unusual`, `higher`, source special cause: the share above 0.25 s is
outside the band of 7 previous days for s0 and for s1. Blast radius: every member in this source,
not one bad instance (with 5 or more members, `fleet` would name outliers).

## 5. Hypotheses and ruling out

Record competing explanations before favouring one, and test each against data that could
refute it.

<!-- call: scenario=triage id=h_load -->
```json
{"tool": "hypothesis_create", "args": {"statement": "A traffic increase drove the http.server latency shift at about 10:00Z"}}
```

<!-- call: scenario=triage id=f_load -->
```json
{"tool": "finding_create", "args": {"claim": "http.server request rate did not change in the last hour against the 4 previous hours (ratio about 0.99, interval spans 1): no traffic increase to explain the latency shift", "scope": {"source": "default", "selector": "http_server_requests_total", "start": "now-1h", "end": "now", "step": "1m", "aggregation": "sum by (service_name) of rate"}, "evidence": ["$verdict.roles.rate.evidence.0"], "hypothesis": "$h_load.hypothesis", "stance": "against"}}
```

<!-- call: scenario=triage id=h_load_refuted -->
```json
{"tool": "hypothesis_update", "args": {"hypothesis": "$h_load.hypothesis", "status": "refuted", "note": "rate unchanged (common cause) while latency shifted"}}
```

<!-- call: scenario=triage id=h_daily -->
```json
{"tool": "hypothesis_create", "args": {"statement": "The slow requests are the normal load of this hour of the day"}}
```

<!-- call: scenario=triage id=f_daily -->
```json
{"tool": "finding_create", "args": {"claim": "For each of service_name s0 and s1 the share of requests above 0.25 s in the last hour is above the range of the same hour on the 7 previous days", "scope": {"source": "default", "selector": "http_server_request_duration_seconds_bucket", "start": "now-1h", "end": "now", "step": "1m", "aggregation": "share of observations above 0.25 s, per service_name"}, "evidence": ["$seasonal.series.0.share_over.evidence", "$seasonal.series.1.share_over.evidence"], "hypothesis": "$h_daily.hypothesis", "stance": "against"}}
```

<!-- call: scenario=triage id=h_daily_refuted -->
```json
{"tool": "hypothesis_update", "args": {"hypothesis": "$h_daily.hypothesis", "status": "refuted", "note": "both members unusual against 7 previous days"}}
```

## 6. The finding

Mark the onset on the latency panel, then record the main finding with its evidence, scope and
reference window:

<!-- call: scenario=triage id=onset -->
```json
{"tool": "annotate", "args": {"kind": "event", "panel": "$group.roles.duration.panel", "at": "$verdict.roles.duration.onset.at", "label": "latency shift onset (09:56-10:02Z)"}}
```

<!-- call: scenario=triage id=f_main -->
```json
{"tool": "finding_create", "args": {"claim": "From about 10:00Z (09:56-10:02Z) the share of http.server requests slower than 0.25 s rose from 1.2% to 5.4% against the 4 previous hours; a 5xx burst followed from 10:20Z; request rate unchanged", "scope": {"source": "default", "selector": "http_server_request_duration_seconds_bucket", "start": "now-1h", "end": "now", "step": "1m", "aggregation": "share of observations above 0.25 s, service_name s0+s1 summed", "baseline_start": "now-5h", "baseline_end": "now-1h"}, "evidence": ["$verdict.roles.duration.evidence.0", "$verdict.roles.duration.evidence.1", {"kind": "panel", "panel": "$group.roles.duration.panel"}, {"kind": "annotation", "annotation": "$onset.annotation.id"}], "caveats": ["overdispersed: intervals use effective n", "onset interval approximate", "order is timing, not cause"]}}
```

Read: the result lists `sources: ["special_cause"]`; report it with the finding.

<!-- call: scenario=triage id=h_cause -->
```json
{"tool": "hypothesis_create", "args": {"statement": "The 10:00Z latency shift caused the 10:20Z 5xx burst (for example timeouts)"}}
```

The order supports it only as timing; it stays `proposed`. Stop here: a special cause is
localised (onset, scope: both members, all of this source), the two cheap alternatives are ruled
out, and what would separate the remaining hypothesis (failed-request latency with
`split_outcome`, deploy or dependency events at 10:00Z) is the next step to offer.

## 7. Report

"http.server (both service_name members, source default), last hour against the 4 previous
hours at 5% family-wise over 3 signals: from about 10:00Z (09:56-10:02Z) the share of requests
slower than 250 ms rose from 1.2% to 5.4% (f3, special cause). A 5xx burst followed from about
10:20Z (10:17-10:22Z); request rate did not change. Ruled out: a traffic increase (f1, h1
refuted) and the normal load of this hour (f2, h2 refuted: outside 7 previous days). Open: did the
latency shift cause the errors (h3, timing only). Next: `split_outcome` on the latency, and what
changed at 10:00Z."
