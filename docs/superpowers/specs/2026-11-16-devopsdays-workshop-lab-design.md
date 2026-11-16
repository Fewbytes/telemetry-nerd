# DevOpsDays TLV 2026 workshop lab environment — design

Status: draft, 2026-11-16. Deck: `docs/into-presentation/`. Epic: see beads (`workshop`).

## 1. Goal

A 90-minute hands-on session ("Telemetry Nerd: a power tool for power telemetry users"). Attendees
look at a conventional Grafana or Kibana panel, see how it misleads, then ask Telemetry Nerd the same
question and compare. To do that we need data sources **we control**, filled with generated
metrics and logs where every misleading pattern is **planted on purpose and its ground truth is
known**. Grafana Play and the other public sources (see
`2026-10-01-public-test-sources.md`) can't give us that: we can't plant faults there, the answers
are unknown, and a full room would hit someone else's rate limits.

Decisions (user, 2026-11-16):

- **Shared hosted stack plus a local fallback.** One box we run, read-only for attendees, who
  install only Telemetry Nerd. The same compose file runs on a laptop as a fallback and as a
  take-home.
- **Elasticsearch + Kibana run on the hosted box only.** They need about 1.5–2 GiB RAM, too heavy
  for laptops. The local fallback has Grafana and VictoriaMetrics only.
- **Attendees bring their own Claude Code.** People without access pair up.

## 2. Shape

```
                      hosted box (podman compose, Caddy TLS)
  attendee laptop    ┌───────────────────────────────────────────────────────────┐
  ┌──────────────┐   │  Caddy ── grafana.<domain>  Grafana (anonymous Viewer)    │
  │ Claude Code  │   │       │                      ├─ DS: VictoriaMetrics       │
  │ + TN plugin  │──────────┘                      └─ DS: Elasticsearch         │
  │ TN daemon    │   │  Caddy ── kibana.<domain>   Kibana (anonymous, read-only)│
  │ browser :7070│   │                                                           │
  └──────────────┘   │  workshop-gen ── backfill ──▶ VictoriaMetrics (not public)│
                     │       └── live /metrics ◀── VM scrape (15 s)              │
                     │       └── access logs ───▶ Elasticsearch (not public)     │
                     └───────────────────────────────────────────────────────────┘
```

- **One public front door: Grafana.** Telemetry Nerd already connects through a Grafana datasource
  proxy (`source_connect(grafana=..., uid=...)`): Prometheus-compatible directly, and ES through
  `_msearch` (`docs/install.md`). Attendees therefore need only the Grafana URL, and VM and ES are
  never exposed. Kibana is public for viewing only.
- **A named source preset**, e.g. `devopsdays`, works like `grafana-play`, so the lab prompt
  "connect to devopsdays" works. The local fallback uses the same preset with a URL override
  (`http://127.0.0.1:3000`).
- **Read-only throughout:** anonymous Viewer in Grafana, an anonymous read-only role in Kibana, and
  no write path reachable from outside.

## 3. The world: "Nerd Shop"

A small synthetic fleet with plausible names and labels, so the Grafana dashboards look normal:

| Service | Pods | Role in the labs |
|---|---|---|
| `frontend` | 8 | error ratio (autoscaled axis), arrivals |
| `checkout` | 12 | latency histogram (p99 lie), CPU spikes (rollup lie), Little's law |
| `payment` | 6 | dead exporter (gap lie), downstream fault in the Lab 3 incident |
| `cart` | 4 | background, a red herring |

Metrics (Prometheus naming, classic histograms, TYPE/HELP/UNIT exported):

- `http_server_requests_total{service,pod,route,code}` (counter)
- `http_server_request_duration_seconds_{bucket,sum,count}{service,pod,route}` (histogram)
- `http_server_active_requests{service,pod}` (in-flight gauge)
- `container_cpu_usage_seconds_total{pod,container}`, `container_memory_working_set_bytes`,
  `kube_pod_container_resource_limits{resource}` (usage against limits)
- `up{job,instance}`, so a dead exporter is visible as a scrape failure, not only as missing series

Logs (ES index `nerdshop-access-*`, `@timestamp`): one document per sampled request, with
`service, pod, route, status, duration_ms, start/end`. These give Kibana its panels and
Telemetry Nerd its ES path: TDigest percentiles, and `in_flight` for Little's law, where the
in-flight gauge is the independent instrument.

**Time model:** 14 days of history with diurnal and weekly seasonality, then a live tail. The
generator is a **deterministic function of (seed, time)**. Backfill evaluates past times and
imports them (`/api/v1/import/prometheus`, as `devtools/synthetic.py` does); live mode exports
the current time on `/metrics`. The two meet at "now" with no seam. The Little's law service
reuses the `queue_sim` engine (M/G/c, faults, ground truth JSON) for its live part.

## 4. Planted lies

Each lie has a conventional panel, the Telemetry Nerd view, a ground truth and a place in the
session. Ground truth goes in `truth.json` (written by the generator) and in the instructor answer
key.

| # | Lie | Planted | Conventional panel (Grafana / Kibana) | Telemetry Nerd shows | Session |
|---|---|---|---|---|---|
| 1 | Mean rollup erases a spike | `checkout-7` CPU saturates for 40 s every ~3 h | Grafana, 7-day CPU panel, auto step, `rate()` → mean | min/max envelope; spike counted exactly | talk demo + Lab 2 |
| 2 | Gap bridged / dead pod disappears | `payment-3` exporter dead for 38 min yesterday at a fixed time; again for 10 min each hour | Grafana `avg by (service)` panel with connect-nulls: the dead pod drops out and the average doesn't move | fleet view "6 pods, 1 silent"; gap shown and named | talk demo + Lab 2 |
| 3 | Autoscaled axis | `frontend` error ratio moves from 0.20% to 0.21% | Grafana, autoscaled y | contextual y-range; change sized against noise | talk demo + Lab 2 |
| 4 | p99 of p99s | `checkout-11` is slow (×4) and gets 2% of traffic | Grafana `avg(histogram_quantile(0.99, …))` | p99 from the merged histogram with its interval; the gap as a number | talk demo + Lab 2 |
| 5 | Average hides the tail | bimodal latency: 3% of requests take a slow path | Kibana Lens "average `duration_ms`", auto interval | ES percentiles (TDigest) and the distribution | talk demo |
| 6 | The incident (Lab 3) | every hour at :20 for 12 min, `payment` latency ×3 → `checkout` in-flight rises, arrivals flat; Little's law holds. Red herring: a `cart` deploy annotation at :18 | Grafana "Nerd Shop overview" dashboard | hypotheses tested, scoped findings, RED verdicts | Lab 3 |

Run the hourly incident on a loop so a fresh one with a known answer is always on screen, whenever
the session starts or overruns. Lie 5 needs verification against the current Kibana version: confirm
the default aggregation and interval actually mislead; if they don't, change the panel to the
common hand-built one and say so on the slide.

## 5. Dashboards

- **Grafana:** a provisioned folder "Nerd Shop (conventional)". One dashboard per lie, built the
  way people normally build them (defaults, autoscale, `avg by`), plus an overview for Lab 3.
  Each panel description says "this panel is wrong on purpose, see Lab N".
- **Kibana:** a saved-objects `.ndjson` with a Lens dashboard for lie 5 and a logs view for Lab 3.

## 6. Hosting

- One cloud VM (4 vCPU / 8 GiB is a starting guess, confirm by load test). Podman compose, with
  Caddy for TLS on `grafana.<domain>` and `kibana.<domain>`.
- Capacity: about 60 attendees each running a Telemetry Nerd daemon against one Grafana. Load-test
  with the lab prompts replayed by N concurrent daemons (bead). Use the Grafana datasource proxy
  timeout and VM `-search.max*` limits as the guard rails; Telemetry Nerd's public-source
  politeness (`1h9.7`) applies.
- Up from the day before; backfill on first start; the live generator under a restart policy;
  health check on the instructor laptop.
- The local fallback is `just workshop-up` with profile `local` (VM + Grafana + generator, no
  ES/Kibana), about 400 MiB. It's also in PREREQS as optional.

## 7. Testing

- Generator unit tests: deterministic for a seed; backfill and live agree at the seam; every
  planted fault is present at its `truth.json` window.
- Ground-truth integration tests (`-m integration`, VM container): Telemetry Nerd's own tools
  recover each planted lie. The envelope catches the spike, the fleet view names the silent pod,
  the merged p99 differs from the averaged one by the planted amount within its interval, and
  Little's law is consistent during the incident. These double as the dry run of the lab prompts.
- A rehearsal on the hosted box with the deck, before the event.

## 8. Out of scope

Traces, attendee write access, per-attendee data isolation, and Telemetry Nerd authentication (the
daemon stays on each attendee's loopback).
