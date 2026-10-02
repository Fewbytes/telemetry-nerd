# External metric context (2as.18)

The repo's code, docs and dashboards say what a metric is. Claude reads the files in its own
session and sends their text (`catalog_context(source, files=[{path, text}], dry_run)`); the daemon
reads no files. <= 50 files, <= 1 MB each, text only, nothing executed.

## Origin `context`
Precedence: user > claude > stats > **context** > pack > metadata > rule. More specific than a generic
pack, but a declaration that may be stale, so observed behaviour (stats) outranks it. Confidence by
evidence: code registration 0.85, dashboard 0.7, docs table 0.6. A scan never overrides it.

## Extractors (`catalog/context.py`, pure, deterministic)
- Python `prometheus_client` (stdlib `ast`): Counter/Gauge/Histogram/Summary/Info/Enum; names composed
  like the library (namespace_subsystem_name, `_unit`, `_total` on counters, histogram/summary members,
  `_info`); module string constants resolved; help, label names, unit, buckets, line.
- Python / JS-TS / Go OpenTelemetry instruments, with the OTLP->Prometheus name translation (dots to
  underscores, UCUM unit suffix, `_total` for monotonic counters).
- Go prometheus client (`CounterOpts{Name, Help, Namespace, Subsystem}`, Vec/promauto; no `_total` added).
- Grafana dashboard JSON: a panel speaks for a metric only when every expression is that one metric
  (plain selector, or rate/increase of a counter); its unit id maps to a canonical unit (a per-second
  unit only over a rate), its description to the metric's description.
- Markdown metric tables (name, type word, description).
Names built at runtime (f-strings, concatenation, variables) are skipped and reported, never guessed.

## Writing
Claims `description`, `type` (counter/gauge on an exact name; histogram/summary on a base name; none
for Info/Enum or members), `unit`, with citation `code: path:line (python prometheus_client Counter)`
etc. Only for metrics this source has; definitions it lacks come back as `unmatched` (a lead).
Disagreement inside the call (code vs docs) keeps the higher confidence and says so. One bulk write,
one internal `catalog.context_ingested` event, idempotent; type/unit changes re-derive open panels'
axis units.

## Contradictions -> findings
If a context claim on type or unit disagrees with the top declared/pack/stats claim, one system finding
per (metric, field, other origin) with a new evidence kind `claim` (`ClaimRef`: source, metric, field,
origins): "code: q.py:2 says type is 'gauge', but source metadata says 'counter'". A name-rule
disagreement is a correction, not a contradiction. The UI renders claim evidence as a link to the catalog.

## Not done
YAML collector renames/unit conversions and other languages (follow-up); importing a repo in one step;
file watching; label names and bucket bounds have no catalog field yet (they are in the extractor output).
