# Metric card (2as.12, spec §6.4)

Collapsible card under a panel; nothing is fetched until it is opened, and an open card refetches
on catalog events (`catalogSeq`: any `catalog.*`, `relation.*`, `binding.*` event).

## Payload (`GET /api/panels/{id}/card`)
One section per catalogued metric in the panel's expression (max 3, a plain selector first):
- **fields** type, unit, bounds, additivity (series/time), role, description (+ histogram family
  when claimed): winning value, origin, confidence, basis, all competing claims, `conflict`
  (descriptions never conflict: prose). Core fields are rows even when unclaimed, so they can be
  filled in place.
- **relations, bindings, gaps**: touching the metric; gaps are the ones its bindings raised.
- **operating profile** summary (window, range, seasonal, stale) or why there is none (a refusal
  reason, or "being computed now": the request starts a background compute).
- **data quality**: query step, configured series interval, series in the panel, empty-bucket %, and the
  series interval (`scrape_interval_ms`) measured by one live query per metric (cached 1h; failures become a reason).
  Counter resets and catalog-wide cardinality are reported as **not measured** (2as.6; discovery's
  cardinality is not stored) rather than left blank.
An unlearned source yields `learned: false` and the card says so.

## User writes
`POST /api/catalog/claims {source, metric, field, value}` -> origin `user`, confidence 1 via
`catalog_claim` (validated; unknown metric 404). Confirm = write the shown value (pins it against
re-learning and Claude); Edit = write a new one. The event is intentional: Claude receives
"user set unit of m on s to ...". Enum fields use a select; unit/role/description a text input.

## Axis follows the catalog
A `unit` or `type` claim re-derives the axis unit and provenance of open panels using that metric
(`panel.unit_refreshed`), except a unit stated explicitly when the panel was shown
("provided by ..."), which stays. Claude's catalog writes behave the same.

Out of scope: deleting claims, editing relations/bindings, spectrum thumbnail (M4), whole-catalog
view (2as.13).
