# T0 rules (2as.4)

Deterministic catalog claims from source metadata and naming conventions (spec §4.2).

- `catalog/rules.py: derive_claims(name, info, families)`: origin `metadata` (declared TYPE/HELP/UNIT,
  confidence 0.9, units normalized; "1" is dimensionless and claims nothing) and origin `rule`
  (name conventions, 0.4-0.8). Rules claim only what a convention reliably says: `_total` ->
  counter; `_seconds/_milliseconds/_microseconds/_nanoseconds/_bytes/_ratio/_percent` -> unit and
  bounds; ratio/percent intensive; counters additive; `_info` -> additivity none, **no type**
  (Prometheus exposes `*_info` as gauge); a plain `_seconds`/`_bytes` metric gets no type guess;
  dotted OTel names are treated as underscored; `traces_spanmetrics_latency` special-cased.
- Histogram families from `Discovery.histograms`: classic `[base_bucket, base_sum, base_count]`
  (0.95) on the base and each member; native `[base]` (0.9).
- `WorkspaceService.catalog_learn(source, discovery)`: relearn inventory (`complete=False` when
  discovery was truncated), bulk-write claims, one `catalog.learned` event. Writes are
  idempotent (unchanged value/confidence/citation keeps its timestamp).
  `TelemetryService.learn(source)` = discover + catalog_learn. Auto-learn on connect and the MCP
  tool belong to 2as.9.
- Charts: `infer_unit(expr, lookup)` keeps the expression/transform logic; per-name unit, type
  and provenance come from `catalog_facts` (catalog winners, else rule-only name facts). Counter
  detection for the rate transform uses the resolved type, so a declared gauge named `_total`
  is not turned into `/s`. `y.unit_provenance` names the winning origin ("set by user",
  "source metadata", "inferred from metric name", ...).

## Public-source check (Grafana Play, 3535 metrics)
`scripts/learn_public.py play` found two rule flaws that unit tests had not: `_info` type claims
contradicted every declared gauge (34 conflicts), and `*_nanoseconds_total` fell back to `count`.
Fixed; the remaining 6 conflicts are genuine (metadata declares a gauge for a `*_total` name),
which is what 2as.6 should surface. Offline regression: trimmed recorded fixtures in
`tests/fixtures/play`, `tests/unit/test_play_learning.py`.
