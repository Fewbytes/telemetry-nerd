# Demo environment (OpenTelemetry Demo -> Collector -> VictoriaMetrics)

Real microservice telemetry with a fault-injection switch, for developing and evaluating
Telemetry Nerd (spec §9; beads `telemetry-nerd-1h9.4`, scenarios in `1h9.5`). For day-to-day
work on learning/catalog/charts the hosted copies of this demo on Grafana Play are free
(`docs/superpowers/specs/2026-10-01-public-test-sources.md`); use the local one when you need to
**toggle faults** and know the ground truth.

```
just demo-up            # trimmed stack (17 containers, ~1.0-1.2 GiB resident)
just demo-up full       # + frontend-proxy (envoy), flagd-ui, image-provider
just demo-down          # stop; metrics volume kept
just demo-reset         # stop and wipe the metrics volume
just demo-ps
```

Own compose project `tn-demo`, own ports and volume (`tn-demo_tn-demo-vmdata`); it runs next
to `deploy/dev` (VM on :8428) and never touches it. Needs ~1.2 GiB RAM free in the podman VM
(+~0.3 GiB for `full`) and ~5 GB of disk for images the first time (see Sizes).

## Endpoints (all 127.0.0.1)

| What | Where |
|---|---|
| **VictoriaMetrics** (point Telemetry Nerd here) | `http://127.0.0.1:8429` |
| Shop frontend | `http://127.0.0.1:18080` |
| Locust UI (load generator) | `http://127.0.0.1:8089` |
| flagd OFREP (HTTP evaluation) / gRPC | `http://127.0.0.1:8016` / `127.0.0.1:8013` |
| OTLP/HTTP into the Collector (extra senders) | `http://127.0.0.1:4318` |
| `full` only: shop + `/loadgen/` + `/feature/` (flagd-ui) via envoy | `http://127.0.0.1:18081` |

Port 8430 is deliberately not used (the queue-sim work runs a VM there); the `spike` profile
(`just demo-up spike`, see the spike note) adds VMs on 8441/8442.

```
uv run telemetry-nerd serve --port 7070 --data-dir /tmp/tn-demo --source-url http://127.0.0.1:8429
# then the MCP tools: source_learn, binding_suggest (kind RED), query, ...
```

## What runs

Trimmed (default): `victoriametrics`, `otel-collector`, `flagd`, `valkey-cart`, `astronomy-db`
(Postgres, which product-catalog reads from in 3.x), and the services `frontend`, `checkout`,
`cart`, `payment`, `product-catalog`, `currency`, `email` (checkout needs it), `shipping` +
`quote` (checkout and frontend need them), `ad` and `recommendation` (the frontend calls them on
the home/product pages; dropping them makes the baseline error), and `load-generator` (Locust,
20 users, `DEMO_USERS=N just demo-up` to change; the browser/Playwright traffic is off, it
needs ~1 GiB). Not included: kafka, accounting, fraud-detection (so the `kafkaQueueProblems`
flag does nothing), the LLM agent/chatbot, Grafana/Jaeger/OpenSearch/Prometheus.
Images are pinned to the OpenTelemetry Demo 3.1.0 release; the flag file and the Postgres init
script are vendored from that tag.

## Pipeline

Services -> OTLP -> Collector (`otelcol.yml`) -> OTLP/HTTP -> VictoriaMetrics
`/opentelemetry/v1/metrics`, VM started with `-opentelemetry.usePrometheusNaming`. Traces are
not stored; the Collector's `span_metrics` connector turns them into
`traces_span_metrics_{calls_total,duration_milliseconds_*}` (RED per service/span).
Why OTLP and not remote write: `docs/superpowers/specs/2026-10-03-demo-pipeline-spike.md`
(only OTLP keeps TYPE/HELP/UNIT in VM; names match the OTel semconv knowledge pack).
Resulting names: `http_server_request_duration_seconds_{bucket,count,sum}`,
`rpc_server_call_duration_seconds_*`, `db_client_operation_duration_seconds_*`,
`jvm_memory_used_bytes`, ...; labels are `service_name`, `service_namespace`,
`http_response_status_code`, `rpc_method`, ...; there is no `job`/`instance`.

## Toggling faults (flagd)

flagd serves `deploy/demo/flagd/demo.flagd.json` (bind-mounted, watched). Services re-evaluate
flags continuously, so a change shows in the metrics within seconds.

```
just demo-flag list                      # flags, default variant, variants
just demo-flag get paymentFailure        # what flagd serves right now (OFREP)
just demo-flag set paymentFailure 50%    # a variant name
just demo-flag set paymentFailure off
just demo-flag off                       # everything off
```

`scripts/demo_flag.py` edits the JSON `defaultVariant` in place (a write-then-rename replace
makes flagd lose its file watch) and `get` asks flagd over OFREP
(`POST http://127.0.0.1:8016/ofrep/v1/evaluate/flags/<name>`, body `{"context":{}}`). With the
`full` profile the same file can be edited in the browser at `http://127.0.0.1:18081/feature/`.
Remember `just demo-flag off` after an experiment: the file is a tracked copy of the upstream
defaults (all `off`).

Flags that act on services in this trimmed stack (variants in brackets). Checked on this stack:
`paymentFailure=100%` gave 0.57 error spans/s on `payment`, `checkout` and `frontend` within a minute
(baseline 0); `adFailure`, `cartFailure`, `adHighCpu` and `loadGeneratorFloodHomepage` also showed.

| Flag | Effect | Shows up as |
|---|---|---|
| `paymentFailure` [10%..100%] | payment `Charge` fails n% of the time | errors on `payment`, `checkout`; `/api/checkout` 5xx at `frontend` |
| `paymentUnreachable` [on] | checkout cannot reach payment | `checkout` errors, no `payment` traffic |
| `cartFailure` [10%..100%] | cart `EmptyCart` fails n% | `cart`/`checkout` errors |
| `productCatalogFailure` [on] | `GetProduct` fails for one product id (`OLJCESPC7Z`), so errors are sparse | errors on `product-catalog`, `frontend` for that product |
| `productCatalogLockContention` [on] | DB lock contention in product-catalog | product-catalog latency up |
| `adFailure` [on] | ad `GetAds` fails ~1 in 10 | `ad` errors |
| `adHighCpu` [on] | CPU burn in ad; the burner threads keep running after the flag is turned off, `podman restart tn-demo-ad-1` ends it | `jvm_cpu_recent_utilization_ratio` 0.66+ (ad container ~110% CPU) |
| `adManualGc` [on] | full manual GCs in ad | `jvm_gc_duration_seconds_*`, ad latency |
| `recommendationCacheFailure` [on] | recommendation leaks cache | recommendation memory/latency growth |
| `intlShippingSlowdown` [5sec, 10sec] | slow shipping for international orders | shipping/checkout latency |
| `emailMemoryLeak` [1x..10000x] | email leaks memory (container limit 120 MB: it gets OOM-killed) | email memory, restarts |
| `loadGeneratorFloodHomepage` [on] | Locust floods `/` | frontend request rate 0.6 -> 1.8 req/s |
| `imageSlowLoad` [5sec, 10sec] | slow images (`full` profile only) | image-provider latency |

No effect here: `kafkaQueueProblems`, `aiRunawayAgent`, `aiSlowResponse`, `emitRawPii`,
`failedReadinessProbe`.

## Scenarios (ground truth for evals)

`scenarios/*.yml` schedule flagd flag changes on the running demo and record what really
happened (spec §9; bead `telemetry-nerd-1h9.5`; consumer: the scenario eval harness, `d77.3`).

```
just scenario-list
just scenario payment-failure            # ~13 min: 5 min baseline, 5 min fault, 3 min cool-down
just scenario payment-failure --scale 0.4  # shorter (observable onset needs >= ~3 min of fault)
uv run scripts/scenario.py show payment-failure   # resolved schedule, no side effects
```

A run: health check (VM up, traffic flowing, no error baseline, all flags off; `--force` to skip) ->
baseline -> scheduled steps at wall-clock time (flag edited via the in-place writer, then polled
over OFREP until flagd serves the variant) -> cool-down -> cleanup (every flag off, sticky
containers restarted) -> each expected signal queried from VM (baseline window vs fault window
after a settle period) -> `scenarios/runs/<id>-<UTC ts>.json` (gitignored). Trimmed samples live
in `tests/fixtures/scenarios/`.

Note the lag: the demo exports metrics and span-metrics every 60 s, so with 2m rate windows the
effect appears 1-3 min after the flag change (and fades 1-3 min after it is reverted). Ground
truth gives the exact apply times plus `tolerance`; judge annotations against those.

Scenario YAML: `id`, `description`, `timing {baseline_s, cooldown_s}`, `steps` (`{at, set: {flag:
"variant"}}`, `{at, restart: [container]}` or `{at, hook: name}`; variants must be quoted strings,
YAML 1.1 turns bare `on`/`off` into booleans), `sticky_restart`, `faults` (per-flag metadata),
`ground_truth` (`root_cause_service/flag/summary`, `affected {origin, propagated,
unaffected_control}`, `tolerance_s`, `settle_s`, `expected_signals`). `scripts/scenario.py` has
`HOOKS` for extra step types (queue-sim, bead 1h9.17, can register a hook and run alongside).

### Ground-truth JSON (`version` 1)

| Key | Content |
|---|---|
| `scenario`, `description`, `version` | scenario id; schema version |
| `run` | `started_at`, `baseline_end`, `finished_at` (epoch s, each with a `_iso` sibling), `completed`, `scale` |
| `fault_window` | `{start, end}`: first fault applied .. last effect end (flag off, or the container restart for sticky faults). Use this for annotation tolerance. |
| `faults[]` | per flag: `flag`, `variant`, `start`, `end`, `effect_end`, `services`, `start_served_at` (when flagd first served it), `sticky` |
| `applied[]` | every step as actually executed: `kind` set/restart/hook, `flags`, `applied_at`, `served_at` |
| `root_cause` | `service`, `flag`, `summary` (what a correct finding names) |
| `affected_services` | `origin` (directly hit), `propagated` (should show the symptom downstream), `unaffected_control` (must NOT be blamed) |
| `tolerance` | `start_s`, `end_s`: allowed distance of an annotation from `fault_window.start/end`, plus a note on why |
| `expected_signals[]` | `id`, `service`, `metric`, `query` (PromQL against `endpoints.victoriametrics`), `direction` up/down/flat, `min_abs_delta` / `min_ratio` / `max_abs_delta`. `flat` signals are controls. |
| `endpoints` | VM, flagd OFREP, frontend; `source_labels.service` = `service_name` |
| `verification` | `all_passed` and `results[]` per expected signal: baseline/fault means and sample counts, `passed`, `detail`, and the `series` (`[epoch, value]`, 15 s step) used as evidence |

### Observed on this stack (300 s fault, 20 users)

| Scenario | Observed (baseline -> fault) |
|---|---|
| `payment-failure` | error spans/s: payment 0.003 -> 0.41, checkout 0.003 -> 0.41, frontend 0.003 -> 0.41; cart stays 0 |
| `cart-failure` | weak but clean: cart and checkout error spans/s 0 -> 0.008 (EmptyCart is rarely hit) |
| `homepage-flood` | frontend request rate 2.1 -> 40 /s (x19), product-catalog 1.1 -> 1.9, frontend event-loop utilisation 0.004 -> 0.034; no errors |
| `ad-high-cpu` | ad `jvm_cpu_recent_utilization_ratio` 0.0004 -> 0.66; ends at the container restart |
| `shipping-slowdown` | shipping `POST /ship-order` p90 2 ms -> 8-12 s (checkout PlaceOrder inherits ~14 s); `/get-quote` unaffected |
| `catalog-lock-contention` | product-catalog p90 4 ms -> 15 s (the histogram's top bucket, so a floor), frontend p90 -> 15 s, frontend completed rate 7.9 -> 3.7 /s; recovery takes minutes |

Probed, not shipped as scenarios: `productCatalogFailure` (no error spans in 3 min; sparse by design),
`recommendationCacheFailure` and `emailMemoryLeak=1000x` (nothing beyond noise within 3 min),
`paymentUnreachable` (not tried). `uv run scripts/scenario.py reverify <run.json>` re-judges a run
against the current YAML using the run's recorded window (VM history), for threshold tuning.

## Scenario evals (bead d77.3, spec §10)

`just eval <scenario>` scores an investigation's workspace against a ground truth
(`telemetry_nerd.evals`; runner `scripts/eval_scenario.py`):

```
just eval payment-failure                      # offline: canned snapshot tests/fixtures/evals/
just eval payment-failure --snapshot build/evals/<run>/snapshot.json --truth <gt.json>
just eval payment-failure --live               # run the scenario (or --attach <run.json>), start an
                                               # isolated daemon on the demo VM, YOU investigate
just eval payment-failure --live --attach scenarios/runs/<run>.json --claude   # SPENDS TOKENS
just eval overload_spike --live --claude --duration 600   # queue-sim: throwaway VM, no demo
```

`--claude` runs `claude -p "/telemetry-nerd:investigate <neutral question>"` from an empty
directory with a staged copy of the plugin (commands, skills, hooks, MCP config: no sources,
tests or ground truth), `--setting-sources project`, `--permission-mode dontAsk`, only the plugin's
MCP tools (+ Skill, ToolSearch) allowed and Bash/Read/Write/Web denied; the bridge reaches the eval
daemon via `TN_DAEMON_URL`. Caps: `--model sonnet`, `--max-turns 40` (hard cap), `--max-budget-usd
5`, `--timeout-s 1800`; a run whose init shows the MCP server not connected is stopped at once.
`--max-turns` limits tool-use rounds (model responses that call tools); Claude Code's reported
`num_turns` counts the prompt plus every tool result, so with parallel calls it can exceed 40
(46 in the 2026-10-03 payment run) without the cap being broken. The harness counts rounds
itself, stops a run past the cap (backstop), and records `caps` (rounds, num_turns, cost against
both caps, what stopped the run) in run.json and the report. Never part of `just test`.

Criteria (pass/fail/n/a each; report.md + report.json in `build/evals/<scenario>-<ts>/`):
findings present, **scoped** (source, selector, step, aggregation, time range in the run, every
entity the claim names covered by the selector or the cited evidence), **evidenced** (a statistic
or an existing panel), uncertainty flags repeated, root cause named by an in-window finding,
controls never blamed, directions consistent with the expected signals, incident findings
labelled with the expected source of variation (special cause for a demo fault), or
undetermined where the op itself labelled the cited statistic so under its cautious model
(principle 16; a claim that still calls it special cause fails), **annotation
onset** within `fault_window.start` ± `tolerance.start_s` and none off target, **zero unscoped
claims** (findings, and sentences of the final answer that state a cause, or that a named service
is absent, citing no f/h/p/a/g id; the analyst's own limits such as "cannot confirm ... because"
are not causes, nor are items listed under a "Not established" / "Unknowns" heading), a root-cause
hypothesis supported, hypotheses blaming a control refuted or inconclusive. Bold = d77
acceptance; the exit status is 0 only when they pass. Text rules are word heuristics; each flag
lists the sentence it fired on. Root-cause terms and entity names right after a negation ("at
unchanged arrival rate", "not a payment fault") do not name the root cause; a hypothesis whose
first-named entity is a control blames it. In queue-sim, a fault on every pod makes the service
itself an origin entity, covered by any expression on the one-service VM.

Each live run keeps the raw stream (`transcript.jsonl`) in its run directory;
`uv run scripts/trim_eval_run.py <run dir> <name> "<note>"` turns it into regression fixtures
(`tests/fixtures/evals/<name>.{snapshot,truth}.json`, `<name>.stream.jsonl`).

## Verifying Telemetry Nerd against it

After ~10 minutes of traffic (daemon started with `--source-url http://127.0.0.1:8429`):
`source_learn` learns 222 metrics (families `otel_sdk`, `rpc_server`, `rpc_client`,
`http_server`, `http_client`, ... with the semconv pack applied), and `binding_suggest`
(kind RED) proposes `RED:otel_http` (`http_server_request_duration_seconds`), `RED:otel_rpc`
(`rpc_server_call_duration_seconds`) and `RED:spanmetrics` (`traces_span_metrics_calls_total`),
all joined on `service_name`. `query` works on them, e.g.
`sum(rate(rpc_server_call_duration_seconds_count{service_name="checkout"}[1m]))`,
`histogram_quantile(0.9, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="frontend"}[$__rate_interval])))`
(the p99 form is flagged `low_count`: 20 users give only ~3 requests/s; raise `DEMO_USERS`
or use a coarser step).

## Extra scrape jobs (queue-sim hook, bead 1h9.17)

VictoriaMetrics loads every `deploy/demo/scrape.d/*.yml` (Prometheus `scrape_configs:` files);
see `scrape.d/README.md`. A queue-sim exporter on the host is reachable from the VM container
as `host.containers.internal:<port>`. To run it as a compose service in the same project add a
`queue-sim` service (profile `queue-sim` is already included in `just demo-down/demo-reset`)
and a scrape file targeting `queue-sim:<port>`. `VictoriaMetrics` reloads with
`curl -X POST http://127.0.0.1:8429/-/reload`.

## Sizes

Image sizes on disk (uncompressed): load-generator 1.7 GB, frontend 668 MB, postgres 487 MB,
ad 443 MB, collector 370 MB, payment 279 MB, email 201 MB, cart 150 MB, quote 139 MB,
currency 125 MB, recommendation 110 MB, flagd 110 MB, shipping 62 MB, valkey 46 MB,
checkout 38 MB, product-catalog 38 MB, VM 34 MB: about 5.0 GB in all; `full` adds
frontend-proxy 195 MB, flagd-ui 194 MB, image-provider 95 MB.
Resident memory with 20 users (`podman stats`, ~10 min): ~1.0-1.2 GiB for the 17 containers
(largest: ad ~250 MB, cart ~115, frontend ~110, load-generator ~100, VM ~60-85, collector ~60);
container limits in `compose.yml` follow upstream's and sum to ~2.7 GiB.

## Troubleshooting

- `podman compose` here delegates to docker-compose. podman splits exec-form healthcheck
  commands on spaces and eats quotes, so healthchecks are written without either.
- If the podman VM wedges: `podman machine stop && podman machine start`, then restart whatever
  was running (`just dev-up`, `just demo-up`).
- Port 8080 is not published (frontend is on 18080) to stay clear of other local services.
