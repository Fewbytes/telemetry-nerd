# T2 Claude learning (2as.9)

Claude interprets what T0 (rules, metadata, packs) cannot, using in-session inference (no daemon
model calls), through MCP tools plus a skill and command.

## Tools
- `source_learn(source)`: discover + T0 + packs; returns counts, caveats, family overview.
- `catalog_search(source, query?, prefix?, needs_review?, limit)`: compact rows (type/unit/role/
  bounds and their winning origin, conflicts, `reviewed`, `hot`). Hot = metrics in this source's
  workspace datasets, sorted first. `needs_review` = no role from pack/claude/user, or conflicts.
- `catalog_get(source, metric)`: resolved fields, all claims with origin/confidence/basis, conflicts.
- `catalog_write(source, claims[])`: <= 200 per call; `{metric, field, value, confidence, basis}`.
  Origin is always `claude`; `basis` required (stored as citation); confidence in (0, 0.9];
  per-item rejection (never aborts the batch); results say `effective` or `outranked_by`.
  A claim outranked by user (or any higher origin) is stored but loses; user entries are never
  overridden. Each accepted claim is an internal `catalog.claimed` event.
- Deferred: `catalog_relate` (relations/model bindings are 2as.8); `bounded_by` is writable now.

## Plugin
`skills/metric-learning/SKILL.md` (procedure, evidence-to-confidence table, rules on timestamps,
counters, bounds, additivity) and `commands/learn.md` (`/learn [source] [family]`; the plugin is
named `telemetry-nerd`, so the namespaced form follows the plugin name, see 3fs.3).
MCP INSTRUCTIONS gained the learn-before-charting habit.

## Tests
Service/MCP level agent simulation: learn -> search (hot first, needs_review) -> write -> get;
shadowing by user; outranking pack/metadata/rule with the conflict kept; every rejection case;
event class; limits. Lint: skill/command only name real tools and carry the discipline rules.
The skill's judgment itself is not exercised without a live session.
