# Catalog view (2as.13)

A `Catalog` view next to `Panels` (`#/catalog`; evidence links like `#/panel/p3` switch back; the
panels stay mounted but hidden so plots keep their state).

## Server (`GET /api/catalog`, SQL-side)
`catalog/browse.py` filters and pages in SQLite, never loading claims outside the page. A window
function finds each field's **winning** claim with the Python resolver's order (origin rank,
confidence, recency), so origin and confidence filters are about what the catalog shows, not about
any claim that merely exists (tested against the resolver with Hypothesis; a 20k-metric catalog pages
in <= 5 statements).
Parameters: `source`, `q` (name or description; LIKE wildcards literal), `prefix`, `origin` (winner of
type/unit/role/bounds), `max_confidence` (a key-field winner below it), `conflicts` (claims for a
field disagree; prose never conflicts), `findings` (a scan filed a finding), `reviewed=yes|no` (a
pack/Claude/user role and no conflict), `removed`, `sort` (name | weakest | conflicts), `offset`,
`limit` (<= 100). Response: `total`, rows (key-field winners with origin and confidence, conflicted
fields, finding ids, scan verdict, reviewed), and whole-source `summary` counts.
`GET /api/catalog/{source}/{metric}` returns the card's per-metric section
(`WorkspaceService.metric_section`, now also used by the panel card).

## UI
Source picker, debounced search, prefix, origin, reviewed, weak (< 0.6), conflicts, findings,
removed, sort, 50-row pages. A row expands into `MetricSection` (extracted from the metric card so
both share one editor): every field with competing claims, Confirm / Edit writing origin `user`
through `POST /api/catalog/claims` (intentional: Claude hears it; panel axes follow). The list and
an open row refresh on any catalog event (`catalogSeq`).

Out of scope: learning from the UI, deleting claims, bulk edits, editing relations/bindings.
