# Relations and model bindings (2as.8, spec §3.4)

## Relations
Typed edges `derived_from, part_of, same_quantity, upstream_of, bounded_by, correlated`. One claim per
`(level, source, subject, kind, object, origin)` with confidence, basis, params; the highest origin
(user > claude > stats > pack > metadata > rule), then confidence, then recency decides. A claim
can be a **retraction**: a winning retraction removes the edge from queries while history stays, so a
wrong pack edge is correctable. `contested` flags a lower-ranked disagreement.
Rules: endpoints must exist (catalog inventory or dataset ids), no self edges, symmetric kinds
(`same_quantity`, `correlated`) canonicalize endpoint order, `correlated` needs
`{coefficient, lag_ms, scope}` and is capped at 0.7 for Claude (evidence, never truth).

`bounded_by` is **only** a relation now (the catalog field from 2as.5 is removed): packs keep the
`bounded_by = [...]` shorthand, which emits relation claims (origin `pack`) during learn, only
between metrics the source actually has.

## Bindings
Role registry: `littles_law {arrival_rate, latency, concurrency}`, `RED {rate, errors, duration}`,
`USE {utilization, saturation, errors}`. A binding is `(level, source, kind, key)` (key = service or
resource) with `roles {role: metric | null}` and `join_on` labels; claims per origin, same
resolution and retraction. Every role must be present; `null` = no signal.

**Gaps:** when the winning claim has a null role, a Gap is raised through the existing gap machinery
with a suggestion from a per-(kind, role) table (e.g. `littles_law.concurrency` -> gauge
`<key>_active_requests`). Once per (binding, role) (remembered in `catalog_binding_gaps`);
retracted bindings raise none; gaps are never deleted.

## Levels, events, tools
Same tables for catalog level (metrics of a source) and workspace level (datasets). Events
`relation.claimed`, `binding.claimed` (user: intentional; Claude/system: internal). MCP:
`catalog_relate` (batched, per-item results, `retract`), `catalog_bind`, `catalog_relations`;
`catalog_get` includes touching relations/bindings. Claude's writes need a basis, confidence <= 0.9,
origin claude, and report `effective`/`outranked_by`.

Out of scope: running models (Little's law checks, fits: M4), relation import from external context
(2as.18), UI.
