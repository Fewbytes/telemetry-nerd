# Highlights (3fs.5)

Either side can draw attention to a panel, annotation, hypothesis, finding or gap. Pure UX:
nothing is persisted beyond the event log (same as `focus.changed`).

## Decisions
- Several highlights at once, each with a default expiry of 5 min; either side clears early.
  Explicit `seconds=0` means until cleared. User pins default to until cleared.
- Stateless daemon: events only, no table, no snapshot change. A fresh page load starts empty.
- User highlight with a note is **intentional** (reaches Claude via channel); without a note it
  is **ambient** (digest line). Claude's highlights and all unhighlights are **internal**.
- Expiry is client-side: `receipt time + ttl_ms` (immune to daemon/browser clock skew).

## Daemon / MCP
- Events `object.highlighted` (object_id; payload `note: str|None`, `ttl_ms: int|None`) and
  `object.unhighlighted` (object_id).
- `WorkspaceService.highlight(object_id, actor, note=None, ttl_ms=300_000)` /
  `unhighlight(object_id, actor)`; object must exist (p/a/h/f/g, same rules as thread anchors).
- `classify(actor, type, payload=None)`: payload-aware for user `object.highlighted`.
- `format.py`: intentional `user highlighted p3: "note"`; ambient digest `user highlighted p3`.
- MCP `highlight(object, note?, seconds=300)`, `unhighlight(object)`.
- HTTP `POST /api/highlights {object, note?}` (user, ttl until cleared),
  `POST /api/highlights/{id}/clear`.

## UI
- `lib/highlights.ts`: pure reducer `applyHighlightEvent(state, event, now)`; re-highlight
  replaces the entry; `panel.closed` clears that panel's highlights; `expire(state, now)`.
- Targets get `.highlighted` accent (Claude purple, user orange). Claude's highlight scrolls the
  target into view once, only when off-screen.
- Strip above the layout: author badge, chip (click flashes target), note, ×.
- `PinButton` on panel header, finding card, hypothesis: inline optional note, Enter pins;
  pinning a pinned object clears it.
- Hover/flash helpers move from `RefText` to `lib/refHighlight.ts`.

## Tests
Python: service events, classify/format, unknown-object rejection, MCP tools, HTTP routes.
Vitest: reducer (replace, clear, expiry, panel close). Playwright: API highlight shows in strip
and on target, × clears, user pin with note reaches the channel.
