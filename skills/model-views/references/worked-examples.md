# Worked examples

Each `json` block marked `<!-- call: ... -->` is one MCP tool call, run in order by
`tests/unit/test_model_views_skill.py` against seeded data through the real MCP path. The
statements under each call are asserted there too. Timestamps are of the fixture clock.

## A. RED: a latency shift, then an error burst

Fixture: a service `http_server_requests_total` / `http_server_request_duration_seconds` with two
members (`service_name` label), a latency shift at minute 20 and a 10x error burst at minutes
40-47 of the last hour. The catalog has been learned (`source_learn`).

Propose, narrowing to RED:

<!-- call: scenario=red -->
```json
{"tool": "binding_suggest", "args": {"source": "default", "kind": "RED"}}
```

Review: one suggestion, `RED:otel_http`, key `http.server`, `join_on` `["service_name"]`, nothing
unfilled. `rate` and `errors` are `ambiguous` (one request counter; errors are a `label_split` on
the 5xx status label, the alternative being the histogram `_count`): check that the label exists
with `query` before accepting. `duration` is the histogram base name, confidence 0.9, no
alternatives.

Confirm, stating what was checked:

<!-- call: scenario=red -->
```json
{"tool": "binding_accept", "args": {"source": "default", "id": "RED:otel_http", "basis": "service_name present on all three metrics; 5xx split of the request counter checked with query; latency is a classic histogram"}}
```

Draw the group (the binding key `http.server`, not the suggestion id):

<!-- call: scenario=red -->
```json
{"tool": "show_binding", "args": {"source": "default", "kind": "RED", "key": "http.server", "range": "1h", "step": "1m"}}
```

Read: group `pg1`; `rate` is a rate (`p1`), `errors` is an `error_ratio` (`p2`, errors over
requests with a Wilson band), `duration` is a `distribution` drawn as a heatmap (`p3`); no gaps.
The `notes` repeat that the status label name is a convention.

Judge, against the 4 preceding windows:

<!-- call: scenario=red -->
```json
{"tool": "binding_verdict", "args": {"source": "default", "group": "pg1", "reference": "previous"}}
```

Read and report:

- `summary.first` is `duration`: a `shift` with onset 10:00Z, interval 09:56-10:02Z. `errors`
  moved next: a `burst` with onset 10:20Z, interval 10:17-10:22Z. The intervals do not overlap,
  so the order is claimed. `rate` is `no_change`.
- The duration verdict is the share of requests above 0.25 s (the bucket edge where the reference
  share above is nearest 5%; coarse buckets leave it at 1.2%): 1.2% in the reference, 5.4% now.
  It is not a percentile.
- `family`: 3 roles judged, 5% family-wise. `reference.label`: "previous windows", 4 cycles.
- The duration role carries the `overdispersed` caveat and the errors role `noisier_than_reference`
  (its share is noisier now than in the reference, so the larger spread is used): the intervals
  already account for both, so say so rather than quoting the odds ratios as exact.

An answer built from this: "Against the 4 previous windows at 5% family-wise over 3 signals, the
share of requests slower than 250 ms rose from 1.2% to 5.4% (intervals and n from `level` and
`evidence`) from about 10:00Z (09:56-10:02Z, approximate); the
5xx share rose in a burst from about 10:20Z (10:17-10:22Z). The onset intervals do not overlap, so
the latency change came first; that is timing, not proof of cause. Request rate: no change
detected." Then `finding_create` with the `evidence` objects as returned.

## B. Little's law: consistent, then hidden queueing

Fixture: one instance, 10 workers, 9.5 requests/s, exponential service of mean 1 s, scraped every
15 s. Three signals: `http_requests_total` (arrivals), `http_request_duration_seconds`
(histogram `_sum` / `_count`) and `http_server_active_requests` (in-flight gauge).

When the timer starts at arrival (it covers the queue), the check agrees:

<!-- call: scenario=littles-ok -->
```json
{"tool": "check_littles_law", "args": {"arrival_rate": "http_requests_total", "latency": "http_request_duration_seconds", "concurrency": "http_server_active_requests", "start": "now-1h", "end": "now", "window": "5m"}}
```

Read the discrepancy first: L - lambda x W = -0.16 requests (L 22.9 vs lambda x W 23.0; R 0.99,
measurement interval -6% to +5%); per window R 0.84-1.16. Verdict `consistent`: no systematic
offset, no transient window; W is 2.4 s (service plus queueing). Common cause: at about 2860
requests per window L and lambda W fluctuate +-8% (no warning: below 10%). Assumptions:
`arrivals_vs_completions` and `warmup` are `assumed`, the rest `ok`; repeat the assumed ones in the
answer.

When the latency timer starts only when a worker picks the request up, the queue is invisible to
W, so W is 1.0 s while requests actually spend 2.4 s in the system:

<!-- call: scenario=littles-queue -->
```json
{"tool": "check_littles_law", "args": {"arrival_rate": "http_requests_total", "latency": "http_request_duration_seconds", "concurrency": "http_server_active_requests", "start": "now-1h", "end": "now", "window": "5m"}}
```

Read the discrepancy first: L - lambda x W = +13.3 requests (L 22.9 vs lambda x W 9.54; R 2.40,
measurement interval 2.27-2.52). Verdict `L_high`: `classification.systematic` is an offset of
2.03 (1.76-2.30) in 10 of 12 windows, source measurement system (instrumentation / model mismatch,
not the process); the hints name queueing before the timer starts, latency on a subset and a
broader gauge. Two windows are `transient` against that level (special cause, `phase: other`,
not at a load peak, beyond the windows' own +-43% spread): at rho 0.95 the hidden queue has
excursions the timer never sees. State the
implied unmeasured time as a conditional estimate: if the excess is queueing before the timer,
L / lambda - W = 22.9 / 9.55 - 1.0 = 1.4 s per request; the check alone cannot rule out the other
causes. Cite the `littles_law_discrepancy` and `littles_law_systematic_offset` statistics.

Draw the panel for the user:

<!-- call: scenario=littles-queue -->
```json
{"tool": "show", "args": {"dataset": "d4", "question": "Does measured concurrency match throughput times mean latency?", "mark": "littles"}}
```

A percentile is refused, with a hint to record a histogram:

<!-- call: scenario=littles-queue expect=error -->
```json
{"tool": "check_littles_law", "args": {"arrival_rate": "http_requests_total", "latency": "http_request_duration_seconds{quantile=\"0.99\"}", "concurrency": "http_server_active_requests", "start": "now-1h", "end": "now", "window": "5m"}}
```

Read: the error says the check needs the MEAN latency (`_sum` / `_count`). Do not work around it
with a percentile; say the check cannot run and record the missing histogram with `gap_create`.

## C. Little's law: a load spike measured with an arrivals counter

Fixture: one instance, 4 workers, exponential service of mean 1 s, 2 requests/s (rho 0.5) except
minutes 25-30 at 5 requests/s (rho 1.25): a queue builds, then drains. The request counter
increments on arrival; the latency timer starts at arrival.

<!-- call: scenario=littles-spike -->
```json
{"tool": "check_littles_law", "args": {"arrival_rate": "http_requests_total", "latency": "http_request_duration_seconds", "concurrency": "http_server_active_requests", "start": "now-1h", "end": "now", "window": "5m"}}
```

Read the discrepancy first: L - lambda x W = -0.10 requests (R 0.997, measurement interval -3% to
+3%). Verdict `consistent`: in the peak window the arrivals counter partly compensates (more
arrivals in lambda, fewer completed latencies in W), so L is about lambda x W there and no window
is `transient`. But `classification.promoted` lists the 25-30 min window: at a load peak,
promoted from measurement system to special cause, `deviation: within_measurement`, because the
backlog grew by +376 requests (gauge +376, arrivals - completions +377): 155 times the
steady-state scale (threshold 37.9, Cantelli's bound). Say both: Little's law holds within the
measurement, and the service left steady state toward overload at that peak (special cause;
check saturation). Quote the `reason` and cite the `littles_law_backlog_growth` statistic
(+376 requests with its interval). Without that evidence a load-peak window inside the envelope
would be common cause: "not a signal by itself; watch if it repeats or grows".
