---
description: Learn what a source's metrics mean (units, types, roles, bounds) and record it in the catalog
argument-hint: "[source] [family-prefix]"
allowed-tools: mcp__plugin_telemetry-nerd_telemetry-nerd__source_list, mcp__plugin_telemetry-nerd_telemetry-nerd__source_learn, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_search, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_get, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_write, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_relate, mcp__plugin_telemetry-nerd_telemetry-nerd__catalog_context
---

Learn metrics for `$ARGUMENTS` (first word: source, default `default`; second word: optional
metric family prefix such as `node_cpu`).

Follow the `metric-learning` skill. In order:

1. Run `source_learn` for the source. Report the metric count and any caveats
   (`metadata_coverage`, `cardinality_unavailable`, `metrics_truncated`) in one or two lines.
2. If a family prefix was given, work through `catalog_search(source, prefix=..., needs_review=true)`.
   Otherwise start with the metrics this workspace already queried (they come first in
   `catalog_search(source, needs_review=true)`), then the largest unreviewed families from the
   overview. Do not sweep the whole source.
3. For each batch: `catalog_get` the ambiguous or conflicting metrics, gather evidence, and write
   what you can justify with `catalog_write`, each claim with a `basis`.
4. Finish with a short report: claims written (and any that were outranked), conflicts you found
   in the source, and what you could not establish and would need (source code, docs, the user).
