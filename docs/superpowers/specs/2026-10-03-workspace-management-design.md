# Workspace management: many investigations, one active (beads ugr, 3fs.1)

## Problem

A data dir holds exactly one implicit workspace (`/api/workspace` has no id, `WORKSPACE_ID =
"w1"` in `core/code_ops.py`, the channel tag hardcodes `workspace="w1"`). Starting over means
closing panels one by one; hypotheses, findings and threads cannot be put aside at all. The
user wants to:

- start a **new investigation** from Claude (MCP) or the UI without losing the old one;
- **list and reopen** old investigations; reopening restores panels, findings, hypotheses,
  threads, annotations, time focus and the sources they used (3fs.1 acceptance: "two
  investigations coexist; reopening restores panels, findings and sources");
- put an investigation away (**archive**) so the list stays short.

Out of scope: several users or several concurrent Claude sessions working in different
workspaces at once (bead k01), deleting workspaces (see "Delete"), workspace-scoped catalogs.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | One daemon, one **active** workspace. Every existing MCP tool and HTTP route acts on the active one; no tool gains a `workspace` argument. | Single user (k01 is out). Keeps ~60 tool signatures and the UI's routes unchanged. |
| D2 | **One SQLite file**, a `workspace` column on the workspace-scoped tables (`panels`, `objects`, `events`). Catalog, sources, label listings, operating profiles, consumers and id counters stay global in the same file. | Catalog writes and their events commit in one `log.transaction()` on one connection today (`EventLog.transaction`, `BEGIN IMMEDIATE`). A file per workspace would split that transaction across two connections (SQLite in WAL mode does not commit attached databases atomically). Migration is `ALTER TABLE ADD COLUMN ... DEFAULT 'w1'`: no data moves. |
| D3 | **Object ids stay globally unique**: one `counters` table for all workspaces (p, d, h, f, ...). A new workspace's first panel is e.g. `p58`, not `p1`. | Dataset ids are allocated from the same counter and DuckDB datasets are global; the catalog points at workspace objects (`catalog_findings.finding_id`, `catalog_binding_gaps.gap_id`); channel text, chips and URLs say `p3` without a workspace. Restarting ids per workspace would make every one of those ambiguous. |
| D4 | The workspace a request works in is **pinned at request entry** (a `ContextVar`), not read at each write. A switch only changes what *new* requests see. | Tools await sources for seconds. Without pinning, a `show` started in w1 would write its panel into w2 if the user switched mid-flight. Background tasks (`asyncio.create_task`, `ensure_future`, anyio worker threads) copy the context, so they keep their origin workspace too. |
| D5 | **Event `seq` is global**, every event row carries its `workspace`; **channel cursors stay global** (`consumers` table unchanged). UI-facing reads (`since`, `activity`, snapshot lists) filter by workspace; channel `peek`/`claim` do not. | A user action in w1 just before a switch is still delivered to Claude (no per-workspace cursor that only advances when w1 is active again, no lost question). The UI's monotone `last_seq` guard keeps working across switches. |
| D6 | Switching is **daemon-global and live**: connected UIs get a control frame and reload; Claude gets an intentional `workspace.opened` channel event when the *user* creates or switches. | Matches the single-active model; the user and Claude never look at different workspaces for long. |
| D7 | "New session" = `workspace_create` (create + switch). Old workspaces are kept, listed, reopenable. **Archive**, not delete. | No destructive clear (principle 7: deletes are soft, evidence links never break). |
| D8 | **Sources stay global**; a workspace records the sources it queried (name + spec snapshot). Reopening reports their state and re-attaches a recorded source that is no longer connected when its name is free. | Meets "reopening restores sources" with no per-workspace registry; a source is infrastructure shared by investigations, not investigation state. |
| D9 | **Catalog stays global** (metrics, claims, relations, bindings, samples, families, profiles, label listings). | It is learned knowledge about the systems, not about one investigation (principle 15). |
| D10 | Per-workspace settings (today: `default_range`) move into the workspace row (`settings` JSON). Time focus is the workspace's own `focus.changed` events, replayed as today. | 3fs.1 lists "time focus" as workspace state; the existing settings table has no workspace key. |

## Data model

New table (global):

```sql
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,                -- w1, w2, ... from counters prefix 'w'
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    question TEXT,                      -- the investigation's question, optional
    created_at_ms INTEGER NOT NULL,
    opened_at_ms INTEGER NOT NULL,      -- last time it became active
    archived INTEGER NOT NULL DEFAULT 0,
    settings TEXT NOT NULL DEFAULT '{}',-- {"default_range": "now-3h"}
    sources TEXT NOT NULL DEFAULT '{}'  -- {name: SourceSpec dict} first used in this workspace
);
```

- The **active** workspace is the one with the greatest `opened_at_ms`; it survives a daemon
  restart with no separate state. Ties cannot occur (switch writes `max(now, current + 1)`).
- `workspace TEXT NOT NULL DEFAULT 'w1'` is added to `panels`, `objects`, `events`, with
  indexes `panels(workspace)`, `objects(workspace, kind, anchor)`, `events(workspace, seq)`.
  The default exists only for migrated rows; every insert passes the workspace explicitly.
- Derived, never stored: `last_activity_ms` = `MAX(events.ts_ms)` of the workspace; counts of
  open panels, hypotheses, findings, open threads.
- `SourceSpec` carries no secrets (only `auth_file` / `auth_env`), so recording it is safe.
  The daemon-owned `default` source is never recorded (it is rebuilt from settings).

Scoping rules for the stores:

| Operation | Scope |
|---|---|
| insert (panel, object, event) | tagged with the pinned workspace |
| list / snapshot / brief / `since` / `activity` | pinned workspace only |
| get by id (`get_panel`, `get_finding`, ...) | any workspace (ids are global): evidence links, catalog-raised findings and chips resolve everywhere |
| update by id (close, verdict, status, spec, answered, ...) | refused unless the object is in the pinned workspace: `ValueError("p3 belongs to workspace w1 'checkout latency'; workspace_switch to it first")` |
| channel `peek` / `claim` / `cursor` / `ack`, `last_seq` | global |
| catalog, sources, counters, consumers, profiles, listings | global (unchanged) |

Refusing cross-workspace updates keeps every mutation's event in the same workspace as its
object. Catalog scans dedupe raised findings globally (`catalog_findings`), so a metric card
in w2 can link a finding that lives in w1; the card shows its workspace (get by id works).

### Delete

Not in v1. Deleting a workspace's rows would dangle `catalog_findings` / binding-gap ids and
any cross-workspace evidence link, and leave its datasets in DuckDB unreferenced; doing it
safely needs a reference sweep. Archive covers "make it go away" for now (follow-up bead).

## Runtime

### Active workspace and pinning

`workspace/scope.py`:

```python
_pinned: ContextVar[str | None] = ContextVar("tn_workspace", default=None)

class ActiveWorkspace:
    """The daemon's active workspace id, and the id the current request is pinned to."""
    def __init__(self, initial: str) -> None: ...
    @property
    def active(self) -> str: ...           # daemon-global, what new requests pin to
    def __call__(self) -> str:             # what stores read: pinned, else active
        return _pinned.get() or self._active
    @contextmanager
    def pinned(self) -> Iterator[str]: ... # pin to the active id (request entry)
    @contextmanager
    def using(self, wid: str) -> Iterator[None]: ... # pin to an explicit id (switch, GC)
    def set_active(self, wid: str) -> None: ...      # under a threading.Lock
    def subscribe() / unsubscribe(q) ...            # switch fan-out (UI sockets)
```

Stores (`WorkspaceStore`, `ObjectStore`, `EventLog`) take `scope: Callable[[], str] = lambda:
"w1"` so every existing test that builds a bare store keeps working. `bootstrap.build_service`
and `tests/unit/fakes.make_service` pass one shared `ActiveWorkspace`; `TelemetryService`
holds it as `active`. Nothing else is rebuilt on a switch: every op keeps the same store
objects, which read the scope per statement.

Pin points (each request runs entirely in one workspace):

- **MCP:** `TelemetryMCP.call_tool` wraps `super().call_tool(...)` in `with active.pinned()`.
  The streamable-HTTP session task is long-lived, so HTTP middleware cannot reach tool
  handlers. Sync tools run in anyio worker threads, which copy the context (a test asserts it).
- **HTTP:** a pure ASGI middleware (not `BaseHTTPMiddleware`, whose context handling differs)
  pins `http` scopes. WebSocket scopes are not pinned: `/ws` and `/ws/bridge` are long-lived
  and read `active.active` explicitly per frame.
- **Background:** `create_task` / `ensure_future` / `to_thread` copy the context at creation, so
  auto-profile and similar work stay in their origin workspace. `CodeOps` passes the pinned id
  to `KernelManager.execute` (already keyed by workspace), replacing `WORKSPACE_ID`.

Race behaviour, stated: a tool call that started before a switch finishes in its original
workspace; its result (e.g. `p58`) is real but not on the new board. The switch result tells
Claude which workspace is active, so it can say so. No lock is held across awaits.

### Switch

`core/workspaces.py` `WorkspaceOps` (on `TelemetryService.workspaces`):

```
create(title, question=None, actor) -> meta      # registry insert, then switch(new, created=True)
switch(wid, actor, created=False) -> SwitchResult
    registry.get(wid) (NotFound); reopening an archived workspace unarchives it
    with active.using(wid): log.append(actor, "workspace.opened", wid,
        {"title", "question", "created", "from": previous})
    registry.mark_opened(wid); active.set_active(wid)
    sources = restore_sources(wid)                    # D8
    active fan-out: {"kind": "workspace", "active": meta}
update(wid, title?, question?, archived?, actor) -> meta
    archiving the active workspace is refused ("switch to another one first")
    with active.using(wid): log.append(actor, "workspace.updated", wid, {changed fields})
    fan-out {"kind": "workspace", "active": active meta}   # lists refresh
list(include_archived=False, limit=20) -> {active, workspaces, more}
```

`switch` to the already-active id is a no-op that returns the same result (no event).
Kernels are untouched: each workspace keeps its kernel until the idle reaper stops it.

### Sources on reopen (D8)

- `service.query` / `query_distribution` call `workspaces.note_source(name)` (in-memory set per
  workspace, so the row is written once per source).
- `restore_sources(wid)` returns `[{name, status}]`: `connected`; `restored` (the name was free,
  re-attached from the recorded spec via the `source_connect` path, actor `system`);
  `conflict` (the name is attached to another spec: left alone, `hint` says to reconnect
  under another name); `failed` (with the error).

### Tier-2 run directories

`<data_dir>/runs/<node>` stays one global root (node ids are global). `CodeOps.recover()` and
`keep()` (GC) iterate every workspace with `active.using(wid)` and union the results; without
that, GC in w2 would delete w1's run dirs ("no node in this workspace"). The module docstring
says "in this workspace": it becomes "in any workspace".

## Surfaces

### MCP tools (new)

| Tool | Params | Returns (compact, < 2 KB) |
|---|---|---|
| `workspace_create` | `title: str`, `question: str \| None` | `{workspace: {id, title, question}, previous, url}`; the new workspace is active |
| `workspace_list` | `include_archived: bool = False` | `{active, workspaces: [{id, title, question?, archived?, last_activity, panels, findings, hypotheses, open_threads}], more}` newest activity first, 20 max |
| `workspace_switch` | `id: str` | `{workspace, previous, counts, open_threads, sources: [{name, status}], url}`; hint: `workspace_get` for the brief |
| `workspace_update` | `id: str`, `title?`, `question?`, `archived?: bool` | `{workspace}` |

`workspace_get` keeps its semantics on the active workspace and gains `workspace: {id, title,
question}` at the top of the brief. `workspace_activity` is unchanged for the caller; its
default window becomes "the last 50 events of this workspace" (`EventLog.tail`), because
`last_seq - 50` is wrong once seqs are global.

INSTRUCTIONS gain one bullet: a new, unrelated question is a new workspace
(`workspace_create(title, question)`); `workspace_list` / `workspace_switch` to go back; ids
are global, so `p3` always means the same panel; a tool call never changes the workspace
except these.

### HTTP

| Route | Method | Body / result |
|---|---|---|
| `/api/workspace` | GET | snapshot of the active workspace, plus `workspace: {id, title, question, archived, created_at_ms}`; `last_seq` stays the global max |
| `/api/workspaces` | GET | `?archived=1` includes archived; same rows as `workspace_list` (no 20 cap) |
| `/api/workspaces` | POST | `{title, question?}` → create + switch, actor `user` |
| `/api/workspaces/{id}/open` | POST | switch, actor `user` |
| `/api/workspaces/{id}/update` | POST | `{title?, question?, archived?}` |
| `/api/workspace/default-range` | GET/POST | unchanged, now per workspace |

`/ws` (UI socket): forwards only events whose `workspace` is the active id; replays `since` for
the active workspace; sends `{"kind": "workspace", "active": {...}}` control frames (no seq,
never logged, like presence frames) on every switch or update. `/ws/bridge` needs no change:
`workspace.opened` is an ordinary intentional event in the global stream.

### Events and channel

- `workspace.opened` (INTENTIONAL when actor is `user`; Claude's own switches never echo) and
  `workspace.updated` (AMBIENT when actor is `user`), both logged in the target workspace.
- `Event` gains `workspace: str`; `to_dict()` carries it to the UI and the dispatcher.
- `format_channel`: `meta["workspace"]` is the workspace of the last intentional event (where
  the user is now). When a batch spans workspaces, lines from other workspaces are prefixed
  `[w1]`. `describe_event`:
  - `user opened a new workspace w4 "checkout p99" (question: "why did p99 double at 14:00?"); previous w1`
  - `user reopened workspace w1 "checkout p99"; previous w4`
  - `user renamed w1 to "..."` / `user archived w1`
- The bridge's server instructions drop the literal `w1` (`workspace="w<n>"`, "the workspace
  the event happened in").

### UI

- `lib/api.ts`: `Snapshot.workspace`, `WorkspaceEvent.workspace`, `WorkspaceInfo`,
  `fetchWorkspaces`, `createWorkspace`, `openWorkspace`, `updateWorkspace`; `subscribe` routes
  `kind: "workspace"` frames to `handlers.onWorkspace`.
- `lib/workspace.svelte.ts`: on a `workspace` frame, or a snapshot whose `workspace.id` differs
  from the shown one, clear highlights and reload (a pure `workspaceChanged(prev, next)` helper
  in `lib/workspaces.ts` is the tested seam). Exposes `workspaces` (the list) and refreshes it
  on every `workspace` frame.
- `components/WorkspaceSwitcher.svelte` in `.app-header` left of `ConnectionPill`: a button
  showing the active title; its popover lists workspaces (title, question as tooltip, last
  activity, finding count), each row "open"; a "New investigation" row with an inline title
  input (Enter creates; empty title → "Investigation <date time>"); per row rename (inline)
  and archive; a "show archived" toggle. Keyboard: Esc closes, arrows move. Same Svelte 5
  runes style as `ConnectionPill.svelte` / `HighlightStrip.svelte`; no new dependency.
- Hash links to an object of another workspace (`#/panel/p3`) resolve to nothing on the
  current board; v1 leaves them as is (follow-up: "p3 is in w1, open it").

## Migration

In `open_workspace_db`, after the existing actor/column migrations, idempotent, in one
`BEGIN IMMEDIATE`:

1. `CREATE TABLE IF NOT EXISTS workspaces`; add the `workspace` column to `panels`, `objects`,
   `events` when missing (`PRAGMA table_info`), then the indexes.
2. If `workspaces` is empty, insert `w1`: title `Workspace 1`, `created_at_ms` = earliest event
   or panel time (else now), `opened_at_ms` = now, `settings` = the rows of the old
   `workspace_settings` table as JSON. Seed `counters('w')` to at least 1, so the next id is
   `w2`.
3. `workspace_settings` is left in place, unread (no destructive step).

A fresh data dir goes through the same path and starts with `w1`. Existing data dirs keep
every panel, object, event and catalog row; they all belong to `w1`. Downgrading to an older
daemon is unsupported once a second workspace exists (it would show all workspaces' panels
as one); stated in the release note.

## Principles check

- **7 (single source of truth):** each workspace is one consistent object model; the
  operation layer is shared, scoping is in the stores, deletes stay soft (archive only).
- **1, 2 (evidence, scoped claims):** global ids keep every evidence link and catalog
  reference valid across workspaces; nothing is copied or re-numbered.
- **6 (bulk data never enters context):** new tool results are ids, titles, counts, capped.
- **13 (hypotheses, not verdicts):** archiving hides, never resolves or deletes, hypotheses
  and findings.
- **15 (learned facts carry origin):** the catalog is shared by all workspaces, unchanged.

## Risks

- **Missed scope filter.** A store query that forgets `workspace = ?` leaks another
  workspace's objects into a snapshot. Mitigation: all workspace SQL lives in three store
  classes; an isolation test per list method (two workspaces, assert disjoint).
- **Insert without workspace** silently lands in `w1` (the column default). Mitigation: a test
  creates every object kind in `w2` and asserts the row's workspace.
- **Context not propagated** to a code path that runs outside the pinned task (an
  `run_in_executor` added later). Mitigation: falls back to the active id, which is right
  except mid-switch; tests cover MCP sync tools and `create_task`.
- **Stale UI**: a UI that misses the control frame (socket down) reloads on reconnect and
  compares `snapshot.workspace.id`.
- **Hook-mode delivery** (no channel) is unchanged: the global cursor delivers events of any
  workspace on the next prompt.

## Follow-ups (beads to file)

- Delete a workspace safely (reference sweep: catalog ids, evidence links, datasets).
- Object links across workspaces in the UI ("p3 is in w1: open it").
- `/telemetry-nerd:investigate` offering a new workspace automatically (see plan, Task 12).
- Per-workspace catalog overlays (k01 / multi-user territory).
