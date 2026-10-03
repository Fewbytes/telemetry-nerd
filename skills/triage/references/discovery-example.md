# Worked example: find every service, then the failing one

Each `json` block marked `<!-- call: ... -->` is one MCP tool call, run in order by
`tests/unit/test_plugin_skills.py` against an OpenTelemetry-demo-shaped source
(`tests/fixtures/demo/otel-demo.json`); the statements under each call are asserted there. `$id.path`
stands for a value of an earlier call's result, as in `worked-example.md`. Fixture clock: now = 10:40Z.

Fixture: the demo's 15 services. Only frontend, cart and shipping emit HTTP server metrics; ad,
checkout and product-catalog emit RPC server metrics; payment, email, quote and the rest emit
none of their own. Every service is traced, so the span-metrics connector's
`traces_span_metrics_calls_total` and `traces_span_metrics_duration_milliseconds` carry all of
them. Unknown to the investigator: payment's charge spans fail from 10:15Z, and checkout and
frontend fail with it.

The user says: "order placement is failing since about 10:15". A blast radius built from HTTP and
RPC server metrics sees frontend and checkout fail and never sees payment: the investigation the
d77.3 eval recorded ended with "there is no payment service". This one starts from the services.

## 1. Orient: which words, which services

<!-- call: scenario=payment id=learn -->
```json
{"tool": "source_learn", "args": {"source": "default"}}
```

<!-- call: scenario=payment id=search -->
```json
{"tool": "catalog_search", "args": {"source": "default", "query": "http server request duration"}}
```

Read: words match names and descriptions, so `http_server_request_duration_seconds` is found.
`catalog_family(family="http_server")` would list the whole name group.

<!-- call: scenario=payment id=services -->
```json
{"tool": "entities", "args": {"source": "default", "kind": "service"}}
```

Read: 15 `service_name` values. `payment` reports `traces_span*` families and runtime metrics, no
`http_server` or `rpc_server`; its `bindings` list `RED:spanmetrics` and its `next` is the call
below. `quote` is `active_recent: false`: silent for 20 minutes, which is not the same as healthy.
`coverage.absent_labels` lists the labels searched and not found: a claim that a service does
not exist would have to cite this result and be scoped to these labels and this window.

## 2. Blast radius over every service (RED from spans)

<!-- call: scenario=payment id=suggest -->
```json
{"tool": "binding_suggest", "args": {"source": "default", "kind": "RED", "key": "payment"}}
```

Read: `RED:spanmetrics` joins on `service_name`; `errors` is the `STATUS_CODE_ERROR` split of
`traces_span_metrics_calls_total`. It covers every traced service, so query it for all of them
at once.

<!-- call: scenario=payment id=errors -->
```json
{"tool": "query", "args": {"source": "default", "expr": "sum by (service_name) (rate(traces_span_metrics_calls_total{status_code=\"STATUS_CODE_ERROR\"}[1m]))", "start": "now-1h", "end": "now", "step": "1m"}}
```

Read: three services carry error spans: checkout, frontend and payment. The other twelve have no
error series in the window (absent here means no error span, as the pack says, while their
non-error series report).

## 3. The episode in one service

<!-- call: scenario=payment id=pay -->
```json
{"tool": "query", "args": {"source": "default", "expr": "sum by (status_code) (rate(traces_span_metrics_calls_total{service_name=\"payment\"}[1m]))", "start": "now-1h", "end": "now", "step": "1m"}}
```

<!-- call: scenario=payment id=an -->
```json
{"tool": "analyze", "args": {"dataset": "$pay.dataset"}}
```

Read: the `STATUS_CODE_ERROR` series is `level_shifted` at 10:15Z, `special_cause`; the steps
before its first error span are read as 0 errors where the UNSET sibling reports (a stated
measurement-system assumption). The UNSET series is `stable`: the call rate did not change.

<!-- call: scenario=payment id=panel -->
```json
{"tool": "show", "args": {"dataset": "$pay.dataset", "question": "When did payment's charge spans start failing?"}}
```

<!-- call: scenario=payment id=onset -->
```json
{"tool": "annotate", "args": {"kind": "event", "panel": "$panel.panel", "at": "$an.series.1.stability.shifts.0.at", "label": "onset: payment error spans (level shift)"}}
```

## 4. A cause hypothesis for the episode, and a competitor

The episode is found; now name its cause, not the question. One hypothesis names the subject the
data points at, the other a competing cause that would produce the same symptom.

<!-- call: scenario=payment id=h_cause -->
```json
{"tool": "hypothesis_create", "args": {"statement": "payment is the failing service: its charge spans fail from 10:15Z, and checkout's and frontend's errors are its callers failing with it"}}
```

<!-- call: scenario=payment id=h_alt -->
```json
{"tool": "hypothesis_create", "args": {"statement": "checkout fails on its own from 10:15Z: its PlaceOrder errors do not come from its calls to payment"}}
```

The test that separates them: if checkout fails on its own, its server-span errors exceed its
client-span errors towards payment; if payment is the cause, checkout's client calls to payment
fail as often as payment's own charge spans.

<!-- call: scenario=payment id=kinds -->
```json
{"tool": "query", "args": {"source": "default", "expr": "sum by (service_name, span_kind) (rate(traces_span_metrics_calls_total{status_code=\"STATUS_CODE_ERROR\",service_name=~\"payment|checkout\"}[1m]))", "start": "now-1h", "end": "now", "step": "1m"}}
```

<!-- call: scenario=payment id=panel2 -->
```json
{"tool": "show", "args": {"dataset": "$kinds.dataset", "question": "Are checkout's errors its calls to payment?"}}
```

Read: from 10:15Z checkout's client-span errors (its calls to payment) run at the rate of
payment's server-span errors, about 0.4/s each, and its PlaceOrder server errors match them.

<!-- call: scenario=payment id=f_cause -->
```json
{"tool": "finding_create", "args": {"claim": "payment's charge spans began to fail at 10:15Z: its error-span rate shifted from 0 to about 0.39/s while its call rate stayed stable", "scope": {"source": "default", "selector": "traces_span_metrics_calls_total{service_name=\"payment\"}", "start": "now-1h", "end": "now", "step": "1m", "aggregation": "sum by (status_code) of rate over 1m"}, "evidence": ["$an.series.1.stability.shifts.0.evidence", {"kind": "annotation", "annotation": "$onset.annotation.id"}], "hypothesis": "$h_cause.hypothesis", "stance": "for"}}
```

<!-- call: scenario=payment id=f_alt -->
```json
{"tool": "finding_create", "args": {"claim": "From 10:15Z checkout's client-span errors (its calls to payment) occur at the rate of payment's server-span errors, about 0.4/s each, and checkout's PlaceOrder errors match them: checkout's failures are its payment calls failing", "scope": {"source": "default", "selector": "traces_span_metrics_calls_total{status_code=\"STATUS_CODE_ERROR\",service_name=~\"payment|checkout\"}", "start": "now-1h", "end": "now", "step": "1m", "aggregation": "sum by (service_name, span_kind) of rate over 1m"}, "evidence": [{"kind": "panel", "panel": "$panel2.panel"}], "hypothesis": "$h_alt.hypothesis", "stance": "against"}}
```

<!-- call: scenario=payment id=h_alt_refuted -->
```json
{"tool": "hypothesis_update", "args": {"hypothesis": "$h_alt.hypothesis", "status": "refuted"}}
```

<!-- call: scenario=payment id=h_cause_supported -->
```json
{"tool": "hypothesis_update", "args": {"hypothesis": "$h_cause.hypothesis", "status": "supported"}}
```

Report: payment's charge spans fail from 10:15Z (f1, a1); checkout and frontend fail with it
(f2). h1 supported, h2 refuted. Unknown: why payment fails (no deploy or flag change was given;
ask). Had `entities` not listed payment, the report would say "payment was not found under
`service_name`, `service`, `app`, ... in 09:40-10:40Z", citing it, never "there is no payment
service".
