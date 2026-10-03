# Panel time-frame selector (bead telemetry-nerd-aqk)

## Problem

Users can only set a panel's time range at creation (via the question's start/end). After
that, the range is frozen — no zoom/pan, no re-scope from the UI.

Two requirements that pull in different directions:

1. **Smooth UI flow** — zooming in/out on a panel should feel instant, like any other chart
   interaction.
2. **Evidence trail correctness** — panels are event-sourced; annotations, threads, and
   findings anchor to a specific panel. A range change must never silently relocate what
   evidence was pointing at.

## Precedent

`WorkspaceService.reframe()` (`src/telemetry_nerd/core/service.py:943`) is the one existing
flow that changes what a panel shows *and* requires a re-fetch (changing the expr for a
catalog-suggested reframing). It deliberately creates a **new panel** rather than mutating in
place, and is "never applied silently" — the old panel and everything anchored to it is left
exactly as it was.

Pure UI-state changes that don't re-fetch (`select_y_view`, `select_data_view`,
`set_marginal`) mutate the panel's spec in place and log an **ambient** event
(`panel.y_view_selected` etc.) — fine, because nothing evidentiary is at risk; the underlying
data didn't change.

A time-range change re-fetches data, so it belongs with `reframe`, not with `select_y_view`.

## Design: two-tier range change

### Tier 1 — viewport (client-only, ephemeral)

A new client-side concept, `PanelViewport`: `{start_ms, end_ms}`, separate from the panel's
committed `spec.scope.time_range`. Presets (15m/1h/6h/24h/7d), a typed `now-N` box, an
absolute from/to picker, and (later) chart brush-drag all write to the viewport.

- If the new viewport falls entirely inside the panel's already-fetched buckets: pure
  client-side resample, instant, **no backend call, no event, no identity question**. Exactly
  like today's y-axis zoom — view state, not evidence. Reloading/closing the panel resets to
  its committed range.

### Tier 2 — preview (server fetch, still ephemeral)

When the viewport extends beyond the fetched range:

- Client calls `POST /api/panels/{id}/preview {start, end}` (new route).
- Backend re-runs the same `query()` the panel's dataset used (same expr/source/step
  derivation as `meta`), returns `{dataset, summary}`. **Does not** touch the `panels` table,
  does not mutate `spec`, does not append any event — nothing durable has happened.
- Client renders the preview dataset with a "previewing last 6h" badge. Viewport can keep
  moving; previews are debounced, one in-flight preview per panel (supersede, don't queue).
- If the user navigates away or the preview dataset simply ages out of the dataset cache,
  nothing was ever persisted. No cleanup needed.

### Crystallization — only on an evidence-bearing action

The preview becomes a durable panel only when the user does one of:

- Explicitly clicks "keep this range", or
- Adds an annotation, or cites the panel in a finding, while a preview is active.

Either path calls a new service method, mirroring `reframe()`:

```python
@atomic
def rescope(self, panel_id: str, dataset_id: str, actor: Actor) -> ShowResult:
    """Accept a time-range change (bead aqk): a NEW panel over the new range, marked as
    rescoped from this one, which is left exactly as it was. Never applied silently."""
    p = self.workspace.get_panel(panel_id)
    form = AutoForm(
        transform="rescope",
        source_dataset=p.dataset_ids[0],
        reason=f"rescoped from {panel_id}",
    )
    res = self.show(dataset_id, p.question, actor, auto=form, raw_ok=True, **mark_specific_kwargs)
    self.log.append(actor, "panel.rescoped", res.panel.id, {"from": panel_id, "start_ms": ..., "end_ms": ...})
    return res
```

- `mark_specific_kwargs`: for derived marks (spc, seasonal, littles, fleet, spectrogram),
  re-derive the mark's own config for the new range the same way `show()` does for a fresh
  panel (per your answer: re-derive, don't keep fixed). E.g. spc re-resolves its baseline
  window if the old one no longer fits; littles re-resolves its dataset bundle. These reuse
  `show()`'s existing per-mark construction in `service.py` — no new logic there, just routing
  through the same path `reframe()` already uses.
- This is a new panel id. The old panel, and every annotation/thread/finding anchored to it,
  is completely untouched — satisfies the evidence-trail requirement by construction, not by
  a flag or a warning.
- UI swaps the visible panel to the new id in the same visual slot; the old panel remains
  reachable (closable, shows "rescoped from" / "rescoped to" links both ways, same pattern as
  reframe's `reason` text).

### Why not in-place mutation with an "outside range" flag?

That was the original framing in the bead text, and the initial answer to "what happens to
out-of-range annotations" assumed it. Once the reframe precedent surfaced, in-place mutation
of a panel whose data just got re-fetched would be the first case in the codebase where a
re-fetch is applied silently to existing panel identity — a new rule needing its own
justification, not reuse of an existing one. The two-tier model gets the same UX benefit
(quick zoom feels instant and reversible) without touching that invariant: nothing related to
evidence moves because nothing with an existing identity changes until crystallization, and
crystallization always mints a new id.

## Scope for v1

In scope:
- Presets + typed `now-N` + absolute from/to range input (per your answer).
- Workspace-level default time range for new panels (per your answer) — stored on a workspace
  settings row, read by `query`/`show` defaults when the caller doesn't specify start/end.
  Independent of the preview/crystallize flow above.
- Re-derive per-mark config on crystallization for all mark types already covered by `show()`.

Deferred (follow-up bead):
- Chart brush-drag as a viewport input (the viewport model supports it, but the drag
  interaction itself is separate frontend work).

## Components touched

Backend:
- `src/telemetry_nerd/core/service.py` — new `preview()` method (reuses `query()`), `rescope()`
  method (mirrors `reframe()`).
- `src/telemetry_nerd/api/app.py` — new routes `POST /api/panels/{id}/preview`,
  `POST /api/panels/{id}/rescope`.
- `src/telemetry_nerd/core/events.py` — new ambient type `panel.rescoped` in `AMBIENT_TYPES`.
- Workspace settings: new `default_range` field (workspace-level), read in `query`/`show`
  call sites that currently hardcode `now-1h`.

Frontend:
- `ui/src/lib/api.ts` — `previewPanel(id, start, end)`, `rescopePanel(id, datasetId)`.
- New `ui/src/chart/viewport.ts` (mirrors `ui/src/chart/yview.ts` structure) — viewport state,
  in-bounds check against fetched buckets, debounced preview trigger.
- `ui/src/Panel.svelte` — range control UI (presets/typed/absolute), preview badge, "keep this
  range" affordance, crystallize-on-annotate wiring.

## Testing

- Backend: unit tests for `preview()` (asserts no `panels` table write, no event appended) and
  `rescope()` (new panel id, `panel.rescoped` event payload, old panel/annotations/threads
  byte-for-byte untouched) across each mark type (line, fleet, spc, seasonal, littles,
  spectrogram).
- Frontend: viewport unit tests (in-bounds zoom issues zero network calls); e2e test
  (`ui/e2e/time-selector.spec.ts`, mirroring `ui/e2e/yview.spec.ts`) covering
  preset → preview → annotate → crystallize.
