# Catalog store (2as.3)

Per-metric knowledge (spec §4.3) as **claims with provenance**, resolved on read.

- Entry key `(source, metric)`. Fields: type, unit, bounds, additivity_series, additivity_time,
  role, description, histogram_family, operating_profile_ref. Values are validated per field.
- Claim = `(value, origin, confidence, verified_by, citation, ts_ms)`; one claim per
  `(field, origin)`, replaced by the same origin's newer claim, never touching other origins.
- Origin precedence **user > claude > stats > pack > metadata > rule** (`metadata` = declared
  TYPE/HELP/UNIT from `Discovery`; the bead's `empirical` is an alias of `stats`). Winner:
  rank, then confidence, then recency, then value (order-independent). Losing claims are kept;
  `CatalogEntry.conflicts()` exposes disagreements for 2as.6 findings.
- `origin == "user"` iff actor is the user (Claude cannot forge user edits).
- Storage: `catalog_claims` and `catalog_metrics` in the workspace DB; `CatalogStore` takes the
  connection so it can move to its own DB if knowledge should outlive workspaces (3fs.1, k01).
- Re-learn: `relearn(source, names, complete)` returns `{new, removed, returned}`; removed metrics
  are marked absent but keep their claims; `complete=False` (partial discovery) never removes.
- Operations on `WorkspaceService` (atomic + logged): `catalog_claim` (`catalog.claimed`;
  user edits are intentional), `catalog_relearn` (`catalog.relearned`, internal); reads
  `catalog_entry`, `catalog_list`.
- Out of scope: MCP tools (2as.9), UI (2as.13), claim producers (2as.4+), snapshot changes.
