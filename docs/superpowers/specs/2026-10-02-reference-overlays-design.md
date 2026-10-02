# Reference overlays (2as.11, spec §6.2)

Three layers on time panels, switched by `spec.overlays {normal, limit, ghost}` (persisted;
`normal`/`limit` default on where data exists, `ghost` off: it costs a source fetch, and there is no
triage mode in the code yet to default it on).

- **normal band**: per drawn series, from the cached operating profile (never computed in the data
  path). Each bucket is compared with the same hour of the profile's period (hour of week; `band_at`
  with `ts - step`, since buckets carry their end time); a profile without seasonality gives a flat
  band at the pooled envelope (robust p0.5-p99.5 without true extremes). Series match profile series
  by labels ignoring `__name__`; unmatched series get no band and the chip says how many.
- **limit line**: the `bounded_by` metric's own dataset from `spec.y.context.limit` (2as.10), at
  the panel's LOD grid, dashed in a hazard colour.
- **last-week ghost**: the week reference (`ensure_reference`) shifted onto the panel grid with the
  same helper indexed views use, faint dashed in each series' colour; percentile buckets under
  n_min are not drawn. An empty week says "no data one week earlier".

Payload `overlays {flags, normal, limit, ghost}` always carries availability and a reason; data
only for layers that are on. Disabled chips show their reason as the tooltip. API
`POST /api/panels/{id}/overlays`, MCP `set_overlays`, event `panel.overlays_set` (reloads other
tabs). The UI reloads a panel's data when its y context changes (a profile finishing later).
`data-overlays` on the panel lists the layers actually drawn (used by e2e).

Out of scope: a triage skill that defaults the ghost on; per-series toggles.
