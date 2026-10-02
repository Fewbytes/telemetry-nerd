# Telemetry Nerd — MVP Design

- **Date:** 2026-09-30
- **Status:** Draft for review
- **Scope:** MVP (walking skeleton through every layer), SRE persona first, metrics only

## 1. Purpose

Telemetry Nerd is a Claude Code plugin that turns Claude into a telemetry analysis
partner. It connects to metric sources, learns what the metrics mean, and helps a
human explore data through a shared web workspace with scientifically sound graphs
and analyses.

### 1.1 What it is not

It is **not an "AI SRE"**. Claude does not issue verdicts or root causes. Claude is an
analyst's assistant: it proposes hypotheses, gathers evidence for and against, draws
the graphs that make the data legible, and suggests where to look next. Humans draw
conclusions.

### 1.2 Design principles

1. **Evidence first.** Every claim links to evidence (panel, dataset statistic,
   annotation). Claims without evidence cannot be recorded as findings.
2. **Scoped claims.** Every finding carries an explicit scope (source, selector, time
   range, step, aggregation, optional baseline). Claude must not generalize beyond scope.
   Enforced by schema, not by prompting.
3. **Correct over conventional.** When industry-standard practice is wrong or misleading
   (averaging percentiles, peak-eroding downsampling, autoscaled axes, spaghetti charts,
   dual y-axes), we do the right thing and explain why. We model on scientific
   computing (MATLAB, R, epidemiology, physics), not on dashboard tools. Conventional
   views are available only on explicit user request and carry a caveat.
4. **Every number carries its uncertainty and its aggregation.** Intervals, counts,
   min/max envelopes, and representation metadata travel with the data.
5. **Every graph answers an explicit question.** Panels cannot be created without one.
6. **Bulk data never enters Claude's context.** Claude works with handles and compact
   summaries; the server holds the data.
7. **The workspace is the single source of truth.** Claude, the UI, and tier-2 code all
   mutate the same object model through the same operation layer.

### 1.3 Personas

- **MVP: SRE during an incident.** Latency budget in seconds, recent time ranges, fine
  steps, few round trips, "what changed and where".
- **Later: capacity/performance analyst.** Long ranges, heavy compute, fits and
  forecasts. The architecture supports both (async jobs, progressive results, caching);
  the persona sets defaults via workspace `mode` (`triage` | `investigate`).

## 2. Architecture

Modular monolith: one Python (asyncio) server process, a browser UI, and per-workspace
IPython kernel subprocesses for tier-2 code.

```
 Terminal: Claude Code ◄──── channel events ──────┐
        │ MCP (stdio | HTTP)                       │
        ▼                                          │
 ┌──────────── tn-server (Python, asyncio) ────────┴───────┐
 │  mcp/        MCP tools + channel bridge                  │
 │  api/        HTTP + WebSocket (UI, tn lib, future SDK)   │
 │  workspace/  panels, annotations, hypotheses, findings…  │
 │  pipeline/   DAG: nodes = query | op | code → datasets   │
 │  analysis/   tier-1 ops (process pool)                   │
 │  datasets/   DuckDB series cache (/data volume)          │
 │  sources/    adapter interface → PromQL/MetricsQL        │
 │  catalog/    metric semantics, relations, profiles       │
 │  kernels/    IPython kernel subprocess per workspace     │
 └───────┬──────────────────────────▲──────────────────────┘
         │ WS: workspace events     │ HTTP: tn.query/put (Arrow IPC)
         ▼                          │
   Browser UI (TS/Svelte/uPlot)  IPython kernel (subprocess + tn lib)
```

### 2.1 Storage

- **DuckDB** (`/data/series.duckdb`): series cache and derived datasets. Only the server
  process opens it (DuckDB is single-writer). Workers and tier-2 kernels receive Arrow over
  IPC/HTTP and write results back through the server. ASOF JOIN is used for aligning
  series with mismatched timestamps.
- **SQLite** (`/data/workspace.db`): workspace objects, append-only event log, catalog.
- `/data` is a mounted volume in both local and remote deployments.

### 2.2 Series cache

- Key: `(source, normalized query, step)`; values stored in step-aligned time chunks.
- Range requests fetch only missing chunks from the source.
- Chunks older than a settle window (default 5 min, configurable) are immutable; newer
  chunks have a short TTL and are flagged `settling`.
- Size-budgeted LRU eviction. Datasets referenced by findings are pinned and never evicted.

### 2.3 Event model

Every mutation is an event `(seq, ts, actor ∈ {claude, user, code}, type, object_id,
payload)`. Each event is:

1. persisted to the append-only log (replay, audit, evidence trail);
2. broadcast to the UI over WebSocket;
3. forwarded to Claude through the channel if it is a human *intentional* event
   (see §7.2). Claude's own events never echo back.

### 2.4 Deployment

One container image containing the server and the built UI; tier-2 kernels run as
subprocesses inside it. Local: `uvx telemetry-nerd serve` or docker compose. Remote: same image near
the data source, MCP over streamable HTTP. Remote hardening (auth, code isolation) is
out of MVP scope but the seams exist (§6.3).

## 3. Data and workspace model

### 3.1 From metric to dataset

| Layer | Definition | Identity |
|---|---|---|
| **Metric** | Named measurement in a source; semantics live in the catalog | `(source, name)` |
| **Series** | Metric + full label set | `hash(source, metric, labels)`; stable across queries |
| **Signal** `s*` | Reusable named expression + semantics, without a time range ("checkout p99") | workspace object; promotable to catalog |
| **Dataset** `d*` | A signal or node output evaluated over a range and step; a set of series | immutable |

Derived series (from ops or code) have identity `hash(node, output labels)` and lineage
back to input series, so any plotted point can be traced to raw data.

### 3.2 Dataset schema and representation

Buckets are not single values:

```
(ts, series_id, avg, min, max, count, [lo, hi], [q…])
```

Distribution datasets (histograms):

```
(ts, series_id, bucket_lo, bucket_hi, count)
```

Each dataset declares:

- **representation:** `sample` (instant point), `bucket_agg` (aggregated over step),
  `distribution`, or `estimate` (fit/statistic with interval);
- **native resolution** (scrape interval) and **display step**;
- **unit** and catalog reference;
- producing **node**.

A step smaller than the native resolution raises a `fake_resolution` caveat.

### 3.3 Workspace objects

| Object | Prefix | Key fields |
|---|---|---|
| Workspace | `w` | title, mode, focus range, default step, sources |
| Signal | `s` | expression, semantics ref |
| Dataset | `d` | see §3.2 |
| Node | `n` | kind (`query`/`op`/`code`), inputs, params or code, outputs, status, broker call log |
| Panel | `p` | **question (required)**, status (`open` / `answered → f*`), chart spec, datasets, layout slot |
| Annotation | `a` | kind (`event`/`region`/`threshold`/`band`/`note`), anchor (panel or workspace), time and/or value, label, author, links |
| Hypothesis | `h` | statement, status (`proposed → supported/refuted/inconclusive`), evidence for/against, author |
| Finding | `f` | claim, **scope (required)**, evidence refs, caveats, author, verdict (`accepted`/`rejected`/`needs-more`) + comment |
| Fit | `m` | model, inputs with roles, fit scope, params with CIs, goodness, diagnostics, prediction dataset, derived answers |
| Gap | `g` | missing signal, model that needs it, suggested metric (name, type, labels) |
| Thread | `t` | UI conversation anchored to a panel/selection/object |

**Scope:** `{source, selector, time_range, step, aggregation, baseline_range?}`. Rendered
with the claim, e.g. *"p99 ↑2.1× · checkout, pod=~checkout-.* · 14:00–14:30 · 30s step"*.

**Evidence ref:** one of `{panel}`, `{dataset, statistic: {name, value, interval, method,
params}}`, `{annotation}`, `{fit}`.

**Invariants:**

- Datasets and nodes are immutable; new analysis creates new nodes.
- Deletes are soft; evidence links never break.
- Refuted hypotheses and rejected findings remain visible (filtered/collapsed). Ruling
  things out is valuable.
- A statistic without an interval cannot be evidence unless marked `exact`.
- A fit tagged `weak_fit` cannot back a finding.

### 3.4 Relations and model bindings

**Binary relations** (typed edges):

| Kind | Meaning |
|---|---|
| `derived_from` | e.g. rate(counter), histogram_quantile(buckets) |
| `part_of` | errors ⊂ requests (enables ratios) |
| `same_quantity` | client-side vs server-side latency of one call |
| `upstream_of` | topology (manual in MVP) |
| `bounded_by` | physical limit: used bytes ≤ filesystem size, connections ≤ pool max |
| `correlated` | empirical, with lag/coefficient/scope; always evidence, never truth |

**Model bindings** (n-ary, role-based, with `join_on` labels):

- `littles_law {arrival_rate, latency, concurrency}`
- `RED {rate, errors, duration}` per service
- `USE {utilization, saturation, errors}` per resource

Relations and bindings exist at catalog level (between metrics/signals) and workspace
level (between specific datasets). Each carries `origin` (`pack`/`rule`/`stats`/`claude`/
`user`/`empirical`), confidence, and evidence refs.

A binding with an unfilled role automatically produces a **Gap** — this is how Claude
recommends missing instrumentation (e.g. no in-flight gauge → suggest
`http_server_active_requests`).

### 3.5 Models: bindings, checks, fits

- **Binding:** which signals play which roles.
- **Check:** is data consistent with the model? (Little's law: measured L vs λ·W, with
  residual and interval.)
- **Fit:** estimate parameters with CIs and produce predictions with prediction bands.
  Model-specific derived answers, e.g. time-to-threshold **as an interval**, USL peak
  concurrency `N* = √((1−σ)/κ)`.

Fit guardrails: assumption diagnostics are mandatory; extrapolation beyond fit range adds
an `extrapolated` caveat and is rendered hatched; fit scope becomes the scope of any
finding that uses it; low goodness tags `weak_fit`.

## 4. Sources and metric learning

### 4.1 Adapter interface

```python
class Source(Protocol):
    async def discover(self) -> Discovery: ...
        # metric list, metadata (type/help/unit), label names, cardinality,
        # inferred scrape interval
    def fetch(self, expr: str, range: TimeRange, step: Duration) -> AsyncIterator[RecordBatch]: ...
        # rollup buckets: avg, min, max, count — chunked
    def fetch_histogram(self, metric: str, selector: str, range: TimeRange,
                        step: Duration) -> AsyncIterator[RecordBatch]: ...
        # distribution rows; classic `le`, VictoriaMetrics `vmrange`, native histograms
```

- **MVP adapter:** PromQL/MetricsQL (VictoriaMetrics primary; Prometheus/Mimir/Thanos
  compatible).
- **No peak erosion:** VictoriaMetrics uses MetricsQL `rollup()` / `rollup_rate()` to get
  min/max/avg per bucket in one query. Plain Prometheus falls back to
  `{min,max,avg,count}_over_time`. Re-downsampling in DuckDB aggregates correctly (min of
  mins, max of maxes, count-weighted mean).
- **Guards:** max series, max points, timeout. Breaches return explicit errors with hints
  (narrow the selector, coarser step), never silent truncation.
- Claude writes native PromQL/MetricsQL. There is no invented unified query language.

### 4.2 Learning on connect (tiered by cost)

| Tier | Coverage | Method | Cost |
|---|---|---|---|
| **T0 rules** | all metrics | metadata; naming conventions (`_total`, `_seconds`, `_bytes`, `_bucket/_sum/_count`, `_ratio`, `_info`); built-in **knowledge packs** (OTel semconv, node_exporter for MVP; kube-state-metrics, cAdvisor, runtimes later) | free, deterministic |
| **T1 sample stats** | all metrics, short window | negatives, monotonicity, integrality, constancy, resets, range | cheap queries |
| **T1 operating profile** | lazily on first view | long-window coarse rollups (e.g. 1h × 30d): robust range p0.5–p99.5, min/max, hour-of-week seasonal profile; refreshed daily | cheap queries |
| **T2 Claude** | on demand + hot metrics | meaning, role, bounds, relations, proposed model bindings; batched by metric family/prefix from T0/T1 summaries | tokens |

- Hot metrics: those touched in an investigation, plus RED/USE candidates per detected
  service.
- **Contradictions are findings.** A declared gauge that is monotonic, or a counter that
  goes negative, is an instrumentation bug and is recorded with evidence.
- Re-learning on reconnect: diff new/removed metrics, incremental T0/T1.

### 4.3 Catalog entry

`source, metric, type, unit (canonical), bounds (≥0 | [0,1] | [0,100] | none),
additivity (across series; across time), role, description, histogram family members,
operating profile, origin, confidence, verified_by`.

Users confirm or edit entries in the UI; `user` origin always wins. Additivity drives
aggregation rules and the chart validator.

### 4.4 Early spike

Verify whether metric metadata (TYPE/HELP/UNIT) survives OTel Collector → VictoriaMetrics
via OTLP ingest vs Prometheus remote-write, and choose the demo pipeline accordingly.

## 5. Analysis engine

### 5.1 Tier-1 operations

Each registered op declares input/output **representation**, **semantic preconditions**
(checked against the catalog), an **uncertainty propagation** rule, and **unit
propagation** (via `pint`). Invalid ops are refused with a reason (e.g. `rate` on a
gauge, sum across non-additive series, mean of ratios).

MVP op set:

| Group | Ops |
|---|---|
| Transform | `rate`/`increase` (reset-aware), `resample`, `align` (ASOF), `aggregate` across series with spread + outlier detection (median + MAD band), `ratio` (ratio of sums + Wilson interval), arithmetic |
| Distribution | histogram → heatmap, `ecdf`, `fraction_over` (exact at bucket edges, bounded otherwise), `quantile` (with bucket-edge bounds), `compare_dist` (KS / Wasserstein + bootstrap CI) |
| Statistics | `compare_regions` (effect size + CI; Mann-Whitney / bootstrap), `changepoints` (ruptures PELT) |
| Models | `check_littles_law`, `fit linear` (CIs, residual diagnostics, time-to-threshold interval) |

**Autocorrelation:** telemetry samples are autocorrelated; naive tests are overconfident.
CIs and tests use block bootstrap or effective sample size by default.

**Uncertainty defaults:** Wilson intervals for ratios, Poisson intervals for small counts,
bucket-edge bounds for histogram quantiles, cross-series spread for aggregates.

**Histograms first:** when histograms exist, latency is analysed as a distribution
(heatmap, ECDF, exact fraction-over-threshold, distribution-shift statistics). Quantiles
are computed only on request, always with bounds.

**Execution:** pure functions over Arrow/polars in a process pool; memoized by
`(op, params, input dataset hashes)`. Each op returns datasets plus a compact **summary
for Claude** — key statistics, notable points, auto-generated caveats (`low_n`, `gaps`,
`partial`, `extrapolated`, `autocorrelated`, `resets`, `settling`, `fake_resolution`) —
and significance expressed relative to the normal band ("deviation = 0.3× normal band
width").

### 5.2 Tier-2 code execution (no sandbox)

**Decision (2026-10-02): no sandbox in the MVP.** If the daemon runs in its container
image, that container already is the isolation boundary; if it runs on the user's machine,
Claude-written code in a kernel is equivalent to Claude running Python through Bash (minus
the per-command prompt, which is accepted). A daemon crash is possible either way. Docker
sibling containers, `--network none` and a socket broker are deferred to a pluggable backend
for shared remote deployments (§11.3), not built now. Embedded Lua/WASM Python were rejected
(no scientific Python ecosystem / heavy for no gain); `exec` inside the daemon was rejected
because one runaway loop, memory blow-up or native crash would take the daemon down and a
thread cannot be reliably timed out.

- **Engine:** one persistent **IPython kernel per workspace as a subprocess of the daemon**,
  driven with `jupyter_client`; started lazily, state persists between runs, shut down when
  idle. Same interpreter environment as the daemon.
- **Limits:** wall-clock timeout per run (interrupt, then kill + restart); memory limit
  where the OS allows (`RLIMIT_AS` on Linux). Crash or kill → code node `failed` with the
  traceback, kernel restarted; datasets are server-side so only in-memory variables are lost.
  The daemon is never affected.
- **Libraries:** numpy, polars, pyarrow are always there; scipy, statsmodels, scikit-learn,
  ruptures, pywavelets ship as the optional `analysis` extra (`telemetry-nerd[analysis]`;
  a `-full` image tag carries them).
- **Data exchange: Arrow IPC files in a per-run directory** (`/data/runs/<code-node>/`).
  `run_code(code, inputs=[handles])` makes the daemon export the declared input datasets as
  Arrow IPC files (+ meta JSON); in the kernel `tn.dataset(handle)` memory-maps them
  (zero-copy via the page cache). `tn.put(df, meta)` / `tn.put_fit(...)` write Arrow files +
  meta JSON into the same directory; after the run the daemon ingests them, enforces the
  evidence rule and records lineage (inputs = declared, outputs = put). Adapters and
  credentials never reach the kernel. Run directories are kept while their code node is
  referenced (re-run, debugging) and garbage-collected otherwise.
  - Rejected: **shared memory** (segment ownership and leaks on kernel crash; Docker's 64 MB
    default `/dev/shm`; an mmapped file already shares pages); **DuckDB** (the daemon holds
    `series.duckdb` read-write and DuckDB forbids a second process opening it, even
    read-only; a per-run DuckDB export is file exchange in a worse format); **Arrow IPC
    streaming over a socket** (needs a protocol + server endpoint in the daemon loop, a copy
    each way; Jupyter's own channels can't serve mid-run requests because a busy kernel only
    processes replies after the run). Streaming is the upgrade path if mid-run fetches
    (`tn.query`) are needed: it slots in behind the same `tn.dataset`/`tn.put` calls.
  - **Format (settled 2026-10-02, b98.1/.2; `exchange/fmt.py`):** `run.json` (manifest:
    code node, declared inputs; written last), `inputs/<handle>[.<table>].arrow` + `.meta.json`,
    `outputs/<name>[.<table>].arrow` + `.meta.json`, `ingested.json`. Tables: `rows`,
    `series` (labels), `columns` (distribution n). Every file is temp + fsync + rename; an
    output's meta is written last and commits it. Ingest takes only committed outputs of a
    run that succeeded; temp/orphan/size-mismatched files are reported, never ingested.
    Output meta: `like` (inherit step/range/unit/scheme from an input), `representation`
    (`bucket_agg`/`sample`/`distribution`; `estimate` only via `put_fit`), `labels` (series
    label columns), `unit`, `caveats`, `parents`, and `uncertainty {method, level, kind}` with
    `lo`/`hi` columns or `exact` (integral values only); unknown keys or columns are refused,
    not dropped. Outputs inherit their parents' caveats. Derived series id =
    hash(`code:<node>`, labels). Lineage: parents = all declared inputs unless the code
    narrows them explicitly (reads are not tracked: an input can shape a result without
    being read through `tn`). Fits are `estimate` datasets whose params have the evidence
    statistic shape (`interval` or `exact`); diagnostics are mandatory.
- **`tn` API (MVP):** `tn.dataset(handle)` → polars DataFrame (pyarrow on request),
  `tn.inputs` (declared handles), `tn.put(result, meta)`, `tn.put_fit(...)`. No `tn.query` or
  `tn.chart` in the MVP: Claude queries via MCP and passes handles in; outputs are shown with
  `show`.
- **Code nodes:** each run becomes a `code` node with the stored code, its input and output
  dataset handles (lineage), duration and outcome; re-runnable.
- **Evidence rule:** outputs must declare uncertainty or `exact`; otherwise they are tagged
  `no_uncertainty` and are ineligible as evidence.
- **MCP:** `run_code(code, inputs)` returns stdout (truncated), the produced handles, a
  compact summary and the traceback on failure. Bulk data never comes back through it.
- **Promotion path:** useful tier-2 functions become tier-1 ops.

## 6. Charts and UI

### 6.1 Chart spec

Own layered JSON grammar (Vega-Lite-inspired, telemetry-specific marks):

```json
{
  "layers": [
    {"mark": "line+envelope", "data": "d7"},
    {"mark": "fit", "data": "m3", "band": "prediction"}
  ],
  "x": {"field": "ts"},
  "y": {"from_semantics": true, "range_mode": "reference"},
  "facet": {"by": "service", "shared_x": true},
  "annotations": ["a5", "a6"]
}
```

MVP marks: `line+envelope` (default: mean line + min/max band), `band`,
`points+errorbars`, `heatmap`, `ecdf`, `xy` (scatter, color = time), `fit` (curve + band,
extrapolation hatched), `bar` (counts only). Phase 2: `funnel`, `spectrogram`.

**Auto-charting:** `show(signal)` picks the correct form from the catalog (histogram →
heatmap; counter → rate with envelope; error ratio → Wilson band; bounded gauge → fixed
axis). Explicit specs are for custom views.

### 6.2 Y-axis range is contextual

- **`reference` (default):** range = union(current data, operating profile range,
  physical limit via `bounded_by`). Small fluctuations on a large-scale signal look small.
- **`data`:** fitted to current data, always visibly labelled ("y zoomed: view spans 0.2%
  of normal range") with a **y-context strip** showing where the view sits within the
  reference range. Label and strip cannot be disabled.
- **`semantic`:** natural bounds only.
- Never extend below a natural lower bound; bars include zero; lines may omit zero but
  show a "y ≠ 0 origin" marker; auto log scale when positive data spans more than two
  decades.

**Reference overlays** (default on in `triage` mode): seasonal **normal band** (same hour
of week), **physical limit line**, **baseline ghost** (same window last week).

### 6.3 Validator rules

- Units on every axis; natural bounds respected.
- No dual y-axes — use small multiples with shared x.
- No stacking of non-additive quantities.
- No interpolation across gaps.
- Perceptually uniform colormaps only.
- **Series budget (no spaghetti):** ≤5 series → lines; 6–12 → small multiples;
  >12 → group band (median + MAD control band) with only outlier series drawn and
  labelled ("100 series · 3 outliers shown"), or a series heatmap (row per series).

Violations are errors with a rationale. Overrides require `override: {rule, reason}`, only
at explicit user request, and render as a caveat on the panel.

### 6.4 Panel anatomy

```
┌─ Q: Did checkout latency distribution shift after the 14:00 deploy? ─ [open | answered→f4] ┐
│                                                               │▌ marginal    ┌ metric card ┐│
│   main plot + annotations + normal band                       │▌ histogram   │ description ││
│                                                               │▌ (now vs ref)│ type/unit   ││
│                                                                              │ learnings   ││
│                                                                              │ quality     ││
│                                                                              │ spectrum    ││
│                                                                              │ related →   ││
│                                                                              └─────────────┘│
│ source · scope · step · representation · caveats                                            │
│ Next: [break down by pod] [vs last week] [Little's law check] [heatmap] [ask Claude…]        │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Question** header (required) and answer status linking to the finding.
- **Marginal histogram** on the y-axis: current window vs reference window.
- **Metric card** (collapsible): description; type/unit/bounds; learnings with origin and
  confidence, confirm/edit in place; operating profile; data quality (scrape interval, gap
  %, resets, cardinality); spectrum thumbnail (dominant periods); related signals and
  bindings as links; gaps.
- **Provenance footer:** source, scope, step, representation, caveats — always rendered.
- **Follow-up links:** generated from relations and semantics. Deterministic links run a
  tier-1 op directly and open a new panel with a pre-filled question (no Claude round
  trip). "Ask Claude…" sends the panel and a question to Claude. Object IDs (`p3`, `f4`,
  `s2`) are clickable everywhere.

### 6.5 Rendering

- **uPlot** for time series and XY; small custom canvas renderers for heatmaps (and later
  funnel, spectrogram). Svelte + TypeScript shell.
- **Pixel-aware level of detail:** server sends about one bucket per pixel using
  min/max-preserving aggregation (M4-style). Zoom requests finer steps progressively
  (coarse first, refined when ready).
- Synced crosshair and time range across panels.
- **Render budget as a correctness signal:** UI reports render time and points per pixel.
  Budget ≈ ≤2 points/px and ≤100 ms. A breach logs `render_budget_exceeded` against the
  node and falls back to coarser aggregation; E2E tests fail on breach.

## 7. Claude integration

### 7.1 MCP tools

Coarse-grained, schema-enforced, compact results (≤ ~2 KB: IDs, summary, caveats, UI
link; never raw series).

| Area | Tools |
|---|---|
| Sources | `source_connect`, `source_status` |
| Catalog | `catalog_search`, `catalog_get`, `catalog_write` (batched), `catalog_relate` |
| Data | `query`, `op`, `ops_list`, `fit`, `run_code`, `dataset_peek` (bounded rows) |
| Views | `show`, `panel_create` (question required), `panel_update`, `annotate` |
| Reasoning | `hypothesis_create`, `hypothesis_update`, `finding_create` (scope + evidence required), `gap_create` |
| Collaboration | `workspace_get`, `workspace_activity`, `reply` (answer in a UI thread) |

Transports: stdio (local) and streamable HTTP (remote).

### 7.2 UI → Claude

| Class | Events | Delivery |
|---|---|---|
| **Intentional** | ask about selection (brush + question), comment/note, finding verdict, hypothesis status change, user annotation, "ask Claude" link, catalog correction | immediately via channel |
| **Ambient** | deterministic follow-ups taken, panels closed, focus range changes, axis-mode changes | batched; attached to the next intentional event, or via `workspace_activity` |
| **Never** | hover, pan/zoom, crosshair | UI-local only |

Channel message shape:

```
<channel source="telemetry-nerd" workspace="w1" event="ask" panel="p3" thread="t9">
Q: "why the dip here?" · selection 14:02–14:07 · p3 = checkout latency heatmap
ambient: opened p4 (by pod), p5 (vs last week); closed p2
</channel>
```

Claude acts on the workspace and replies in the thread (`reply`), so the answer appears in
the UI; the terminal shows a short version with links.

**Fallback** when channels are not enabled (research preview; requires `--channels`): a
`UserPromptSubmit` hook injects pending UI events into context on the next terminal prompt.

### 7.3 Future: chat in the UI

The workspace API is transport-agnostic. A later Agent SDK front door embeds a Claude
loop in the server and adds a chat panel to the UI without changes to the core.

### 7.4 Plugin layer

- **MCP config:** local stdio (`uvx telemetry-nerd serve`) or remote URL.
- **Skills** (methodology lives here, not in server code):
  - `evidence` — scoping, findings, hypotheses, correct-over-conventional rules;
  - `triage` — SRE flow: scope blast radius, RED per service, changepoints, baseline
    comparison, hypotheses, ruling out;
  - `metric-learning` — T2 inference procedure and confidence discipline;
  - `models` — Little's law, USL, M/M/c, USE/RED: assumptions and validity limits;
  - `charting` — which chart answers which question.
- **Commands:** `/tn:connect`, `/tn:investigate <question>`, `/tn:open`, `/tn:learn`.
- **Hooks:** `SessionStart` (ensure server, print workspace URL), `UserPromptSubmit`
  (channel fallback).

## 8. Error handling

All errors are explicit and typed; nothing fails silently.

| Condition | Behaviour |
|---|---|
| Source down / timeout | typed error to Claude; partial data carries `partial` caveat |
| Limit breach | error with actionable hint (narrow selector, coarser step) |
| Op precondition failure | refusal with reason |
| Tier-2 code crash / timeout | node `failed` with traceback; kernel interrupted or restarted; daemon unaffected |
| Channel disconnected | events queued; hook fallback delivers them |
| Recent (settling) data | flagged `settling` |
| Render budget breach | logged against node; coarser aggregation fallback |

## 9. Demo environment

`deploy/demo/`, brought up with `just demo-up`:

- **OpenTelemetry Demo** (trimmed via compose profiles; ~6 GB RAM for the full stack) with
  its Locust load generator and flagd fault-injection flags.
- **OTel Collector → VictoriaMetrics single-node** (pipeline chosen by the §4.4 spike).
- **`queue-sim`:** a small Python exporter simulating queues with known parameters
  (service rate μ, servers c, USL σ/κ, concurrency) to provide ground truth for Little's
  law checks and, later, fits.
- **Scenarios:** `just scenario <name>` toggles flagd flags on a schedule and writes
  ground truth (fault start/end, affected services) to JSON.

Client data is not available; everything must run on the demo environment. Real sources
plug in later via read-only adapter configuration.

## 10. Testing

| Layer | Content |
|---|---|
| Unit | Ops vs analytic results; property tests (hypothesis): max-of-max and weighted-mean invariance under re-bucketing, Wilson coverage, unit propagation; validator rules; T0 rules |
| Golden | Synthetic generator (known seasonality, changepoints, bimodal histograms, Little's-law triples) → stable op summaries; no Docker |
| Integration | Adapter + cache against VictoriaMetrics (testcontainers): chunked fetch, rollups, histograms, limit errors |
| E2E | Playwright: panel anatomy, render budget assertions, channel round trip |
| Scenario evals | Claude-in-the-loop on scenarios: findings scoped and evidenced, annotations within tolerance of ground truth, zero unscoped claims. Nightly/manual (cost) |

## 11. Scope

### 11.1 MVP (in)

- PromQL/MetricsQL adapter (rollups, histograms); DuckDB cache.
- Learning T0 + T1 + T2; knowledge packs: OTel semconv, node_exporter; catalog view in UI.
- Ops: `rate`, `resample`, `align`, `aggregate` + outliers, `ratio` (Wilson), heatmap,
  `ecdf`, `fraction_over`, `quantile` (bounded), `compare_dist`, `compare_regions`,
  `changepoints`, `check_littles_law`, `fit linear`.
- Tier-2 code execution (IPython kernel subprocess, `tn` lib; no sandbox, see §5.2).
- Marks: `line+envelope`, `band`, `points+errorbars`, `heatmap`, `ecdf`, `xy`, `fit`,
  `bar`. Validator, reference y-range, normal band, series budget.
- Panel anatomy: question, marginal histogram, metric card, provenance, follow-ups.
- Workspace objects: signals, annotations, hypotheses, findings, fits, gaps, threads;
  event log.
- Channel + hook fallback; plugin skills, commands, hooks.
- Demo environment, `queue-sim`, scenarios.

### 11.2 Phase 2

USL, M/M/c, Holt-Winters fits; SPC (Shewhart/EWMA/CUSUM on de-seasonalized residuals);
STL; spectrogram; funnel plot; Python/matplotlib figure export; analyst-mode tuning;
more knowledge packs.

### 11.3 Later

Logs (VictoriaLogs/Loki/Elasticsearch/Quickwit) and other sources (Datadog, CloudWatch,
Grafana); Agent SDK chat in UI; multi-user collaboration (CRDT); authentication; optional
code isolation for shared remote deployment (`--network none` + Unix-socket broker).

### 11.4 Build order

1. Walking skeleton: server, adapter, cache, `query` → `show` → `line+envelope` in UI.
2. Workspace objects, event log, channel.
3. Learning and catalog.
4. Distribution and statistics ops.
5. Tier-2 code execution.
6. Reasoning objects, UX polish, scenario evals.

## 12. Tech stack

- **Server:** Python ≥3.12, uv, asyncio; MCP Python SDK; Starlette/FastAPI for HTTP/WS;
  DuckDB, SQLite, pyarrow, polars; numpy, scipy, statsmodels, ruptures, pint;
  jupyter_client; docker SDK.
- **UI:** TypeScript, Svelte 5, uPlot, custom canvas renderers.
- **Tooling:** just, pytest + hypothesis, testcontainers, Playwright; beads for issue
  tracking.
