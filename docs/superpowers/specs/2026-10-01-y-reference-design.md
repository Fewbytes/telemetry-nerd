# Contextual y range (2as.10, spec §6.2)

`spec.y.context` (YContext) holds what the catalog says about a time panel's y axis; the client
resolves the range from it plus the drawn data.

## Context (server, after `show`, MCP and HTTP)
`TelemetryService.y_context(panel)`:
- **natural bounds** from the metric's catalog `bounds` winner (rule-only name facts when the
  source was never learned); only for a plain selector, or rate/irate/increase of one counter
  (lower bound 0, "counter rate"). Mixed expressions get none (a ratio of two metrics belongs to
  neither's bounds).
- **physical limit**: for a plain selector with a `bounded_by` relation, the bounding metric under
  the same label matchers is fetched over the panel's time range/step as its own dataset
  (`limit.dataset`, reusable by 2as.11 for the limit line); `limit.hi` is its max. Not applied to
  rates of the bounded metric.
- **operating range** from the T1 profile (`ProfileService.ensure`): pooled envelope (or robust
  p0.5-p99.5 without true extremes). `show` waits up to 8s, else the note `profile_pending` and the
  panel re-asks (`POST /api/panels/{id}/y-context`, up to 4 times).
- Failures degrade to notes (`limit_unavailable`, `profile_unavailable`, ...); the panel renders.
- `range_mode` is `reference` iff a limit or profile exists, else `data` (the honest
  "no reference range yet" note remains only then). Event `panel.y_context` triggers a UI reload.

## Modes (server `YMode`, client `resolveY`)
- `reference`: union(data, operating range, limit), padded, clamped to natural bounds. Not
  "zoomed". Without a limit or profile it is just a data fit and labelled as such.
- `semantic`: natural bounds; an open side follows the data; refused (server and client) with no
  bounds.
- `data`: unchanged, but the badge/strip measure against the reference extent when there is one.
- default `auto`: log when all drawn values are > 0 and span more than two decades (labelled
  "log (auto: N decades)"), else reference when a reference exists, else uPlot's own range.
- Lower bound: the catalog's word wins (`none` lifts the data>=0 assumption); with no catalog
  claim the old assumption stays. Upper natural bound clamps too.
- The reference badge shows only for an explicitly chosen reference view; the always-on panel note
  says what the default range includes.

## Out of scope
Drawing the limit line, normal band and baseline ghost (2as.11, reuses `limit.dataset`).
