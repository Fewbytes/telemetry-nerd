# Bucket Merging per Expression Kind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Buckets are combined across time by the rule their expression needs (sample mean,
time mean, sum of tiles, max/min), never by an implicit count-weighted mean; expressions that
cannot be merged locally are re-queried at the coarser step with windows matched to it, or say
they cannot be combined (bead telemetry-nerd-7jme).

**Architecture:** One classifier `merge_kind()` over the existing PromQL helpers (exprkind +
the observed.py walker + companions' tile detection) gives each dataset a `MergeKind`, stored on
`DatasetMeta` at query time. `rebucket`/`lod` take the kind; every caller passes the dataset's
kind (or the unit-preserving variant for analysis coarsening). `summarize` reports per kind.
`ratio`/`quantile`/`unknown` panels are re-queried at the coarser step through `SeriesCache` in
the background (`rewindow` + `fetch_values`); the result is a kept, citable dataset with lineage
to the panel's dataset (foldable/retirable), pushed to the UI by a socket frame. Step-invariant
expressions merge locally as a stated time mean, and the user can force a re-query.

**Tech Stack:** Python (polars, pyarrow, Starlette), Svelte 5 + TypeScript, pytest, vitest,
Playwright; VictoriaMetrics container for integration tests.

**Spec:** `docs/superpowers/specs/2026-10-04-bucket-merging-design.md` (§ numbers below).
Decided 2026-10-04 (spec §11): Q1 local time mean + forced re-query, Q4 validated code
declarations, Q5 principles 10/17 (already in `docs/principles.md`), Q6 re-queries are kept
datasets. Still open: Q2 (sliding count windows, affects Task 2's table) and Q3 (increase label on
zoom, affects Tasks 5 and 10); do not start those parts before they are answered.

## Global Constraints

- Gates after **every** task: `just lint`, `uv run pytest tests/unit -q`,
  `cd ui && npx vitest run`, `just ui-check`. Master stays green.
- Unit and e2e tests use fixtures and fakes only; only `tests/integration` may start VM.
- No second PromQL parser: new code imports the helpers named in spec §3.
- No cache format change: `EXPR_FORMAT` stays `\0v2`.
- `rebucket` keeps a `sample_mean` default until Task 12 removes it; every converted caller
  passes `kind` explicitly.
- MCP payloads stay small (P6): summary growth ≤ 200 bytes at `top=5`, asserted in tests; a
  re-query dataset is a handle, never rows.
- Every merge says what it did (P17): basis/method text names local vs re-queried vs declared.
- Model routing: tasks marked **[Opus]** are hard (classifier correctness, the re-query
  concurrency path, cross-cutting caller semantics); the rest are Sonnet-sized.

---

## File Structure

Backend:
- `src/telemetry_nerd/analysis/exprkind.py`: walker primitives moved from `sources/observed.py`;
  `window_of_call`; `rewindow()`.
- `src/telemetry_nerd/analysis/mergekind.py` (new): `MergeKind`, `merge_kind()`.
- `src/telemetry_nerd/sources/observed.py`, `model/companions.py`, `core/profiles.py`: import the
  moved helpers (no behaviour change).
- `src/telemetry_nerd/datasets/store.py`: `DatasetMeta.expr_template`, `DatasetMeta.merge`.
- `src/telemetry_nerd/analysis/resample.py`: `rebucket(..., kind)`, `lod(..., kind)`.
- `src/telemetry_nerd/core/summary.py`: per-kind summary.
- `src/telemetry_nerd/core/service.py`: `_fetch()` extracted from `query()`; template stored;
  preview/rescope re-expand; `panel_data` kinds, re-query datasets, `force_requery`.
- `src/telemetry_nerd/core/lod_views.py` (new): `LodViews` (in-flight map, supersede, cache peek,
  re-query dataset creation, completion callback).
- `datasets/store.py` `Lineage` and the code-output exchange: validated per-column `merge`
  declarations.
- `datasets/store.py`, `core/workspace_service.py`, `mcp/server.py`, `api/app.py`: soft
  `retired` flag, retire op, folded listing.
- `src/telemetry_nerd/core/panel_payloads.py`, `core/signal_ops.py`, `core/fleet_ops.py`,
  `charts/indexed.py`, `analysis/profile_reference.py`: callers pass kinds.
- `src/telemetry_nerd/api/app.py`: async panel data, `panel.view_ready` frame.

Frontend:
- `ui/src/lib/api.ts` (`merge` on panel payloads, `total`/`tiles` per point),
  `ui/src/lib/panelNotes.ts` (`describeShown`, caveat texts), `ui/src/Panel.svelte` (badge,
  tooltip, refetch on frame), `ui/src/chart/toUplot.ts` (envelope-only series).

Docs: `docs/telemetry-graphing-guide.md` §5, MVP spec §6.5, `skills/charting/SKILL.md`,
`skills/tier2-code/SKILL.md` (merge declarations); `docs/principles.md` P10/P17 are already
updated with the spec.

Tests (new): `tests/unit/test_merge_kind.py`, `test_rewindow.py`, `test_lod_requery.py`;
extended: `test_resample.py`, `test_summary.py`, `test_indexed.py`, `test_profile_reference.py`,
`test_signal_service.py`, `test_fleet_service.py`, `test_cache.py`, `test_time_selector_service.py`; `ui/src/lib/panelNotes.test.ts`;
`ui/e2e/bucket-merging.spec.ts`; `tests/integration/test_merge_vm.py`; `test_dataset_retire.py`;
extended `test_code_outputs.py`.

---

### Task 1: Share the walker primitives

**Files:**
- Modify: `src/telemetry_nerd/analysis/exprkind.py`, `src/telemetry_nerd/sources/observed.py`,
  `src/telemetry_nerd/model/companions.py`
- Test: existing `tests/unit/test_observed.py`, `test_companions.py`, `test_exprkind.py`

**Interfaces:**
- Produces: in `exprkind`: `peel_operands(text) -> (text, masked)`, `top_operands(masked)`,
  `CALL`, `TRAILING_CLAUSE`, `LITERAL`, `SELECTOR_WITH_WINDOW` (moved from `observed.py:105-160`);
  `window_of_call(masked, call_match) -> int | None` (lifted from `_unobserved_rule`,
  `companions.py:259-280`).

- [ ] **Step 1:** Run the existing observed/companions/exprkind tests; record the pass count.
- [ ] **Step 2:** Move the primitives; `observed.py` and `companions.py` import them. Pure move,
  no behaviour change.
- [ ] **Step 3:** Same tests pass, same count. Commit `refactor(exprkind): share the PromQL operand walker (7jme)`.

### Task 2: `merge_kind` classifier **[Opus]**

**Files:**
- Create: `src/telemetry_nerd/analysis/mergekind.py`
- Test: `tests/unit/test_merge_kind.py`

**Interfaces:**
- Produces: `Kind = Literal["sample_mean", "counter_total", "time_mean", "sum", "max", "min",
  "ratio", "quantile", "unknown"]`; `@dataclass(frozen=True) MergeKind(kind, window_ms: int |
  None, tile: bool, requery: Literal["rewrite", "none", "impossible"])` with `to_dict`/`from_dict`;
  `merge_kind(expr: str, step_ms: int, resolution_ms: int, lookup: Callable[[str], Facts]) ->
  MergeKind`.

- [ ] **Step 1: Write the failing tests**: one parametrised table covering every row of spec §3
  (leaves and composition), e.g.

```python
CASES = [
    ("http_inflight", 15_000, "sample_mean"),
    ("http_requests_total", 15_000, "counter_total"),          # catalog type counter
    ("rate(x[1m])", 15_000, "time_mean"),
    ("sum by (job) (rate(x[1m]))", 15_000, "time_mean"),
    ("rate(x[1m]) * 60", 15_000, "time_mean"),
    ("increase(x[15s])", 15_000, "sum"),
    ("increase(x[5m])", 15_000, "time_mean"),                  # sliding: never summed
    ("sum(increase(x[1m])) / 1e3", 60_000, "sum"),
    ("max_over_time(x[5m])", 15_000, "max"),
    ("-max_over_time(x[5m])", 15_000, "min"),
    ("sum(max_over_time(x[5m]))", 15_000, "unknown"),
    ("rate(a[1m]) / rate(b[1m])", 15_000, "ratio"),
    ("histogram_quantile(0.99, sum by (le) (rate(x_bucket[5m])))", 15_000, "quantile"),
    ("x > 5", 15_000, "unknown"),
    ("max_over_time(rate(x[1m])[1h:])", 15_000, "unknown"),
    ('rate(x{path="/a/b"}[1m])', 15_000, "time_mean"),         # '/' inside a label value
    ("x offset 1h", 15_000, "time_mean"),
]
```

  plus `requery` cases: `$__rate_interval` template or a tile inside a ratio → `rewrite`;
  `a / b` of gauges → `none`.
- [ ] **Step 2:** Run: FAIL (module missing).
- [ ] **Step 3:** Implement as a recursive walk over the Task 1 primitives; `unknown` is the
  fallback for anything not proven.
- [ ] **Step 4:** PASS; commit `feat(mergekind): classify expressions by how their buckets merge (7jme)`.

### Task 3: `rewindow` (template expansion, tiles, widening)

**Files:**
- Modify: `src/telemetry_nerd/analysis/exprkind.py`, `src/telemetry_nerd/core/profiles.py`
- Test: `tests/unit/test_rewindow.py`, existing `test_profiles.py`

**Interfaces:**
- Produces: `rewindow(template: str, base_step_ms: int, new_step_ms: int, resolution_ms: int)
  -> str` (spec §4 steps 1-4); `profile_target` uses it for its rate widening.

- [ ] **Step 1:** Golden-string tests: `$__rate_interval` at S; tile `increase(x[15s])` → `[5m]`;
  `rate(x[1m])` at S = 5m → `rate_interval_ms(5m, res)`; `rate(x[1h])` unchanged; `offset`/`@`
  kept; subquery ranges kept; quoted brackets untouched. Profile tests unchanged.
- [ ] **Step 2:** FAIL. **Step 3:** Implement; switch `profile_target.widen` to it.
- [ ] **Step 4:** PASS; commit.

### Task 4: Store the template and the kind on datasets

**Files:**
- Modify: `src/telemetry_nerd/datasets/store.py`, `src/telemetry_nerd/core/service.py`
  (`query()` around `:490-549`; `preview`/`rescope` `:1044-1090`)
- Test: `tests/unit/test_dataset_store.py`, `test_time_selector_service.py`

**Interfaces:**
- Produces: `DatasetMeta.expr_template: str | None = None`, `DatasetMeta.merge: dict | None =
  None`; `merge_of(meta, lookup) -> MergeKind` (stored or computed lazily for old metas);
  `query()` stores both; preview/rescope query `expr_template or expr`.

- [ ] **Step 1:** Tests: an old JSON meta without the fields loads; a fresh query stores the
  template pre-`expand` and the kind; a preview of a `$__rate_interval` quantile at a coarser
  step re-expands the window (the bug in spec §4).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 5: `rebucket` / `lod` by kind **[Opus]**

**Files:**
- Modify: `src/telemetry_nerd/analysis/resample.py`
- Test: `tests/unit/test_resample.py`

**Interfaces:**
- Produces: `rebucket(buckets, new_step_ms, kind: Kind = "sample_mean") -> pa.Table`;
  `lod(buckets, step_ms, rng, width_px, kind="sample_mean")`; for `sum` the output has extra
  columns `total` (Σ avg) and `tiles` (fine buckets with a value) (`TOTAL_SCHEMA`);
  `ratio`/`quantile`/`unknown` → `avg` null, envelope kept.

- [ ] **Step 1:** Tests with the spec §1 numbers: `time_mean` 10/10/30/10 counts 3/3/1/1 → 15
  (today 12.5); `sum` tiles 150/150/450/150 → avg 225, total 900; the (1+2p) fixture reads I;
  `max` → max of fine max; a gap is excluded (never 0); NaN rules unchanged; envelope identical
  for every kind; unknown counts (code) unchanged for `sample_mean`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 6: Summary per kind

**Files:**
- Modify: `src/telemetry_nerd/core/summary.py` (`summarize`, `:178-266`), `core/service.py`
  (`_time_summary`, `:585-597`)
- Test: `tests/unit/test_summary.py`

**Interfaces:**
- Produces: top-level `merge: {kind, tile?, basis}`; per series `mean`/`last`/`total`/`per_s`/
  `total_lower_bound`/`mean_unavailable` per spec §5; caveats `raw_counter`,
  `tiles_extrapolated`, `time_mean_partial`.

- [ ] **Step 1:** Tests per kind, including the size assertion (≤ today + 200 bytes at 5
  series) and `mean: null` for ratio/unknown/code.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 7: Callers use the kind **[Opus]**

**Files:**
- Modify: `core/service.py:2278-2311`, `core/panel_payloads.py` (`reference_series` `:331`,
  `signal_payload` `:376`, `limit_payload` `:480`, profile overlay `:304`),
  `core/signal_ops.py` (`_prepare` `:207-234`), `core/fleet_ops.py:153-156`,
  `charts/indexed.py:21-28`, `analysis/profile_reference.py:21-55`
- Test: `test_indexed.py`, `test_profile_reference.py`, `test_signal_service.py`,
  `test_spectrum.py`, `test_fleet_service.py`, a new `test_panel_payloads_merge.py`

**Interfaces:**
- Consumes: `merge_of(meta, lookup)`.
- Produces: `unit_preserving(kind) -> Kind` (`sum` → mean per tile, i.e. `time_mean` arithmetic;
  others unchanged) used by analysis coarsening, indexed baselines and `hourly_means`;
  `_prepare`/fleet refuse to coarsen `ratio`/`quantile`/`unknown` with the hint "query at a
  coarser step".

- [ ] **Step 1:** One test per site pinning the kind it uses (spec §8 table), including:
  coarsened tiles keep `events_scale` correct (events per coarse step = per-tile mean ×
  step/w); `hourly_means` of a rate panel equals the time mean, and null counts no longer
  weigh 1; the indexed window baseline of a rate panel is the time mean.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 8: Re-query datasets and forced re-query **[Opus]**

**Files:**
- Create: `src/telemetry_nerd/core/lod_views.py`
- Modify: `core/service.py` (extract `_fetch()` from `query()`; `panel_data` re-query branch),
  `api/app.py` (`panel_data` route awaits a bounded `LodViews.get`; `panel.view_ready` frame)
- Test: `tests/unit/test_lod_requery.py`, `test_cache.py`

**Interfaces:**
- Produces: `LodViews.view(panel, meta, kind, step_s, force=False) -> View(status: "requeried" |
  "pending" | "failed" | "local", dataset_id | None, expr_s, reason)`; a completed re-query is
  `datasets.put(..., producer={"kind": "requery", "of", "step", "trigger"}, parents=[panel
  dataset])` with an `internal` event, recorded in the panel's bounded `spec.views`; one
  in-flight task per (dataset, panel), a newer S supersedes; `quantise_step(step_ms, factor) ->
  S` from `_NICE_STEPS`; payload `merge: {kind, how, dataset, expr, step_ms, status, basis}`;
  `NATIVE_POINT_CAP`; `force_requery` on `show`/panel data (MCP) and a POST route for the UI
  action, disabled with the reason when `requery == "impossible"`.

- [ ] **Step 1:** Tests with a fake source counting calls: pending → envelope (or native within
  the cap) → completion frame → requeried from cache with no new source call; another width
  mapping to the same S is a cache hit; a superseded S is cancelled; source error → `failed`,
  `requery_failed`, no line; `requery == "none"` never fetches unless forced, and its basis says
  "local time mean, not re-queried"; forced → one fetch, a dataset, basis "re-queried"; code
  output never fetches (control disabled with the reason); the re-query dataset has parent and
  producer set, an internal event, and is reused from `spec.views` on reload.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 8a: Validated merge declarations for code outputs

**Files:**
- Modify: `src/telemetry_nerd/datasets/store.py` (`Lineage.merge`), the code-output exchange
  (`src/telemetry_nerd/exchange/run.py`, the `tn` declaration), `analysis/mergekind.py`
  (`merge_of` reads it), `core/code_outputs.py` (caveat text names the declared kind)
- Test: `tests/unit/test_code_outputs.py`

**Interfaces:**
- Produces: `validate_declared_merge(kind, statistic, unit, has_counts, representation) ->
  str | None` (refusal reason); allowed: `sample_mean` (with counts), `time_mean`, `sum`, `max`,
  `min`, `histogram` (distribution outputs); refused per spec §3 (non-mergeable statistic, `sum`
  on a ratio, `sample_mean` without counts); undeclared → `cannot_combine`.

- [ ] **Step 1:** Tests: `time_mean` on a percentile column refused with the reason; `histogram`
  on a distribution accepted; undeclared → `cannot_combine`; the caveat text names the kind.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 8b: Fold / retire datasets

Nothing folds or retires datasets today (panels only have `close`, `workspace/store.py:149`);
kept re-query datasets need it (spec §4 dependency).

**Files:**
- Modify: `src/telemetry_nerd/datasets/store.py` (`retired` in meta, `retire(id)`),
  `core/workspace_service.py` (listing folds `producer.kind == "requery"` under the parent,
  hides retired unless cited), `mcp/server.py` (retire op, small result), `api/app.py` and
  `ui/src/Panel.svelte` ("fold re-query views")
- Test: `tests/unit/test_dataset_retire.py`, UI unit test

- [ ] **Step 1:** Tests: retire is soft (`datasets.get` and evidence links still work, P7); the
  listing shows `+N re-query views` per parent; a cited dataset is never hidden; the MCP result
  stays a handle list.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 9: UI data layer and badges

**Files:**
- Modify: `ui/src/lib/api.ts`, `ui/src/lib/workspace.svelte.ts` (frame → refetch),
  `ui/src/Panel.svelte`, `ui/src/chart/toUplot.ts`
- Test: `ui/src/chart/toUplot.test.ts`, `ui/src/lib/workspace.test.ts`

- [ ] **Step 1:** Tests: a payload with `merge.status: "pending"` and no `avg` draws the band and
  no line; `panel.view_ready` for the panel triggers one refetch; tooltip for `sum` shows total,
  tiles and per tile; a `local` time-mean panel shows its note and a "re-query at {S}" action
  that calls the force route.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 10: Labels (`describeShown`, caveat texts)

**Files:**
- Modify: `ui/src/lib/panelNotes.ts`
- Test: `ui/src/lib/panelNotes.test.ts`, `ui/e2e/bucket-merging.spec.ts`

- [ ] **Step 1:** Tests: the spec §6 sentence per kind; texts for `raw_counter`,
  `tiles_extrapolated`, `time_mean_partial`, `merged_plotted_mean`, `cannot_combine`,
  `requery_failed`. E2E on the fixture source: resizing an increase panel keeps its y-values and
  its "per 15 s" label; a ratio panel shows "re-querying" then the line.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; commit.

### Task 11: Docs, skills, MCP hints

**Files:**
- Modify: `docs/telemetry-graphing-guide.md` §5, `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md`
  §6.5, `skills/charting/SKILL.md`, `skills/tier2-code/SKILL.md` (merge declarations),
  `src/telemetry_nerd/mcp/server.py` (query/show docstrings: `merge` in the summary,
  `force_requery`, re-query datasets are citable and foldable). Principles 10/17 already done.

- [ ] Update; `just lint`; commit `docs: bucket merge kinds in the guide, skills and MCP hints (7jme)`.

### Task 12: Integration check and remove the implicit default

**Files:**
- Create: `tests/integration/test_merge_vm.py`
- Modify: `src/telemetry_nerd/analysis/resample.py` (`kind` required)

- [ ] **Step 1:** VM container: jittered counter; local `sum` of 15 s tiles = `increase(x[1m])`
  at 1 m; local `time_mean` of `rate(x[1m])` = VM rollup at 1 m (fully evaluated buckets); a
  ratio re-query = ratio of summed tiles.
- [ ] **Step 2:** Make `kind` required; fix any caller the type checker finds.
- [ ] **Step 3:** All gates plus `uv run pytest tests/integration -q -k merge`; commit.

### Task 13: Simplify pass and close-out

- [ ] Run the simplify skill over the diff; file follow-up beads (binding ratio of summed
  parents; Tier-1 viewport server LOD); close 7jme after merge (epic close criteria).

---

## Order and dependencies

1 → 2 → (3, 4) → 5 → 6 → 7 → 8 → (8a, 8b) → 9 → 10 → 11 → 12 → 13. Tasks 3/4 and 8a/8b are
independent pairs. Tasks 9-10 need 8's payload shape and 8b's listing. Open Q2 gates Task 2's
sliding-window rows; open Q3 gates the `sum` label in Tasks 5 and 10.
