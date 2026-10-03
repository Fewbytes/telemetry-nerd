# Workspace Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Many investigations per data dir: create, list, switch, rename and archive
workspaces from MCP and the UI; one active workspace at a time; reopening restores panels,
findings, hypotheses, threads, time focus and sources (beads telemetry-nerd-ugr,
telemetry-nerd-3fs.1).

**Architecture:** One SQLite file. `panels`, `objects`, `events` gain a `workspace` column;
a global `workspaces` table holds metadata, per-workspace settings and recorded sources.
Object ids and event seqs stay global. An `ActiveWorkspace` holder (a `ContextVar` pinned at
request entry, falling back to the daemon's active id) is passed to the three workspace
stores as `scope`; nothing is rebuilt on a switch. Switching appends a `workspace.opened`
event (intentional for the user, so Claude hears about it) and pushes a control frame to UI
sockets.

**Tech Stack:** Python (SQLite, Starlette, MCP SDK, contextvars), Svelte 5 + TypeScript,
pytest, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-03-workspace-management-design.md` (decisions D1-D10
are referenced below).

## Global Constraints

- Gates after **every** task: `just lint`, `uv run pytest tests/unit -q`,
  `cd ui && npx vitest run`, `just ui-check`. Master stays green.
- Existing tool signatures and HTTP routes do not change (D1). Stores keep working when built
  without a scope (`scope` defaults to `lambda: "w1"`), so existing tests are untouched until
  a task says otherwise.
- Ids stay global (D3): never add a per-workspace counter.
- No destructive migration step: never drop or rewrite existing rows; `workspace_settings`
  stays in place, unread.
- Every insert into `panels` / `objects` / `events` passes the workspace explicitly; the
  column default `'w1'` is for migrated rows only.
- Model routing: tasks marked **[Opus]** are hard (concurrency, migration, cross-cutting
  scoping); the rest are Sonnet-sized.

---

## File Structure

Backend:
- `src/telemetry_nerd/workspace/db.py`: `workspaces` table, `workspace` columns, w1 seed.
- `src/telemetry_nerd/workspace/scope.py` (new): `ActiveWorkspace`, the `ContextVar`.
- `src/telemetry_nerd/workspace/registry.py` (new): `WorkspaceRegistry` (rows, settings,
  sources, counts).
- `src/telemetry_nerd/workspace/store.py`, `workspace/objects.py`, `core/events.py`: scoped.
- `src/telemetry_nerd/core/workspaces.py` (new): `WorkspaceOps` (create/list/switch/update,
  source restore, switch fan-out).
- `src/telemetry_nerd/core/service.py`, `core/bootstrap.py`, `core/code_ops.py`,
  `core/workspace_service.py`: wiring, kernels, GC, brief, activity.
- `src/telemetry_nerd/mcp/server.py`: pinning in `call_tool`, four tools, INSTRUCTIONS.
- `src/telemetry_nerd/api/app.py`: pin middleware, `/api/workspaces*`, `/ws` filtering and
  control frames.
- `src/telemetry_nerd/channel/format.py`, `bridge/proxy.py`: workspace tag, describe lines.

Frontend:
- `ui/src/lib/api.ts`, `ui/src/lib/workspaces.ts` (new), `ui/src/lib/workspace.svelte.ts`.
- `ui/src/components/WorkspaceSwitcher.svelte` (new), `ui/src/App.svelte`.

Docs: `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md` (§2.1, §3.3, §7.1,
§7.2), `commands/investigate.md`, `commands/open.md`, `commands/start.md`,
`skills/triage/SKILL.md`.

Tests (new): `tests/unit/test_workspace_migration.py`, `test_workspace_scope.py`,
`test_workspace_registry.py`, `test_workspace_isolation.py`, `test_workspace_pinning.py`,
`test_workspace_ops.py`, `test_mcp_workspaces.py`, `test_api_workspaces.py`;
`ui/src/lib/workspaces.test.ts`; `ui/e2e/workspaces.spec.ts`.

---

### Task 1: Schema and migration **[Opus]**

**Files:**
- Modify: `src/telemetry_nerd/workspace/db.py`
- Test: `tests/unit/test_workspace_migration.py`

**Interfaces:**
- Produces: table `workspaces(id, title, question, created_at_ms, opened_at_ms, archived,
  settings, sources)`; column `workspace TEXT NOT NULL DEFAULT 'w1'` on `panels`, `objects`,
  `events`; indexes `panels_workspace`, `objects_workspace (workspace, kind, anchor)`,
  `events_workspace (workspace, seq)`; a `w1` row in every db; `counters('w') >= 1`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_workspace_migration.py
import json
import sqlite3

from telemetry_nerd.workspace.db import open_workspace_db

OLD = """
CREATE TABLE counters (prefix TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE panels (id TEXT PRIMARY KEY, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  spec TEXT NOT NULL, dataset_ids TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
CREATE TABLE objects (id TEXT PRIMARY KEY, kind TEXT NOT NULL, anchor TEXT,
  deleted INTEGER NOT NULL DEFAULT 0, created_at_ms INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('claude', 'user', 'system')), type TEXT NOT NULL,
  object_id TEXT, klass TEXT NOT NULL CHECK (klass IN ('intentional', 'ambient', 'internal')),
  payload TEXT NOT NULL);
CREATE TABLE workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO counters VALUES ('p', 3), ('h', 1);
INSERT INTO panels VALUES ('p3', 'why?', 'open', '{}', '[]', 1000);
INSERT INTO objects VALUES ('h1', 'hypothesis', NULL, 0, 900, '{}');
INSERT INTO events (ts_ms, actor, type, object_id, klass, payload)
  VALUES (800, 'user', 'thread.message', NULL, 'intentional', '{}');
INSERT INTO workspace_settings VALUES ('default_range', 'now-3h');
"""


def old_db(path):
    con = sqlite3.connect(path)
    con.executescript(OLD)
    con.close()


def test_existing_rows_belong_to_w1_and_nothing_is_lost(tmp_path):
    old_db(tmp_path / "workspace.db")
    con = open_workspace_db(tmp_path / "workspace.db")
    assert con.execute("SELECT id, workspace FROM panels").fetchall() == [("p3", "w1")]
    assert con.execute("SELECT id, workspace FROM objects").fetchall() == [("h1", "w1")]
    assert con.execute("SELECT seq, workspace FROM events").fetchall() == [(1, "w1")]


def test_w1_row_carries_old_settings_and_earliest_time(tmp_path):
    old_db(tmp_path / "workspace.db")
    con = open_workspace_db(tmp_path / "workspace.db")
    wid, title, created, archived, settings = con.execute(
        "SELECT id, title, created_at_ms, archived, settings FROM workspaces"
    ).fetchone()
    assert (wid, title, created, archived) == ("w1", "Workspace 1", 800, 0)
    assert json.loads(settings) == {"default_range": "now-3h"}


def test_next_workspace_id_is_w2(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    (n,) = con.execute("SELECT n FROM counters WHERE prefix = 'w'").fetchone()
    assert n == 1


def test_migration_is_idempotent(tmp_path):
    old_db(tmp_path / "workspace.db")
    open_workspace_db(tmp_path / "workspace.db").close()
    con = open_workspace_db(tmp_path / "workspace.db")
    assert con.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (1,)


def test_fresh_db_starts_with_w1(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    assert con.execute("SELECT id FROM workspaces").fetchall() == [("w1",)]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_workspace_migration.py -v`
Expected: FAIL (`no such column: workspace`, `no such table: workspaces`).

- [ ] **Step 3: Implement**

In `db.py`: add the `workspaces` DDL to `_SCHEMA`; add a `_WORKSPACE_COLUMN` loop like
`_PANEL_COLUMNS` for `panels`, `objects`, `events`, run **after** `_migrate_event_actors`
(keep `_EVENTS_DDL` in its old shape: the actor rebuild does `INSERT ... SELECT *`); create
the indexes; then `_seed_w1(con)` inside `BEGIN IMMEDIATE ... COMMIT` (ROLLBACK on error):
insert `w1` only when `workspaces` is empty, `created_at_ms = MIN(min event ts, min panel
created) or now`, `settings = json.dumps(dict(workspace_settings rows))`; `INSERT INTO
counters VALUES ('w', 1) ON CONFLICT DO UPDATE SET n = MAX(n, 1)`.

- [ ] **Step 4: Run to verify they pass**, then the full gates.

- [ ] **Step 5: Commit** `feat(workspace): workspaces table and workspace column, existing data becomes w1 (ugr)`

---

### Task 2: ActiveWorkspace and WorkspaceRegistry

**Files:**
- Create: `src/telemetry_nerd/workspace/scope.py`, `src/telemetry_nerd/workspace/registry.py`
- Test: `tests/unit/test_workspace_scope.py`, `tests/unit/test_workspace_registry.py`

**Interfaces:**
- Produces:
  - `ActiveWorkspace(initial: str)`: `.active` (property), `__call__() -> str` (pinned else
    active), `pinned()` and `using(wid)` context managers, `set_active(wid)` (threading.Lock),
    `subscribe() -> asyncio.Queue`, `unsubscribe(q)`, `notify(frame: dict)` (put_nowait to
    every queue, drop on full with a warning, like `PresenceRegistry.changed`).
  - `WorkspaceInfo` frozen dataclass: `id, title, question, created_at_ms, opened_at_ms,
    archived, last_activity_ms, counts: dict[str, int]`, `to_dict()`.
  - `WorkspaceRegistry(con, new_id, clock)`: `create(title, question) -> WorkspaceInfo`,
    `get(wid) -> WorkspaceInfo` (NotFound), `list(include_archived=False) -> list[WorkspaceInfo]`
    (by `max(last_activity_ms, opened_at_ms)` desc), `update(wid, *, title=None,
    question=None, archived=None) -> WorkspaceInfo`, `mark_opened(wid)` (writes
    `max(now, current_max + 1)`), `active_id() -> str` (argmax `opened_at_ms`),
    `get_setting(wid, key, default)`, `set_setting(wid, key, value)`,
    `sources(wid) -> dict[str, dict]`, `note_source(wid, name, spec: dict)` (no-op when
    present), `ids() -> list[str]`.

- [ ] **Step 1: Write the failing tests** (key cases)

```python
# tests/unit/test_workspace_scope.py
import asyncio

from telemetry_nerd.workspace.scope import ActiveWorkspace


def test_unpinned_reads_follow_the_active_id():
    a = ActiveWorkspace("w1")
    a.set_active("w2")
    assert a() == "w2"


def test_pinned_reads_survive_a_switch():
    a = ActiveWorkspace("w1")
    with a.pinned():
        a.set_active("w2")
        assert a() == "w1"
    assert a() == "w2"


def test_using_pins_an_explicit_id_and_restores():
    a = ActiveWorkspace("w1")
    with a.using("w7"):
        assert a() == "w7"
    assert a() == "w1"


async def test_tasks_inherit_the_pin():
    a = ActiveWorkspace("w1")
    with a.pinned():
        seen = asyncio.create_task(_read(a))  # copies the context at creation
    a.set_active("w2")
    assert await seen == "w1"


async def _read(a):
    await asyncio.sleep(0)
    return a()
```

```python
# tests/unit/test_workspace_registry.py (sketch: one test per interface method)
def test_create_allocates_w2_and_list_hides_archived(tmp_path): ...
def test_title_must_not_be_blank(tmp_path): ...
def test_mark_opened_makes_it_active_even_within_one_ms(tmp_path): ...  # fixed clock
def test_settings_are_per_workspace(tmp_path): ...
def test_note_source_records_once_and_never_default(tmp_path): ...
def test_counts_and_last_activity_are_derived(tmp_path): ...  # insert rows by SQL
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_workspace_scope.py tests/unit/test_workspace_registry.py -v` → FAIL (modules missing).
- [ ] **Step 3: Implement** both modules (pure; no service imports).
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `feat(workspace): active-workspace scope and workspace registry (ugr)`

---

### Task 3: Scope the stores and the event log **[Opus]**

**Files:**
- Modify: `src/telemetry_nerd/workspace/store.py`, `src/telemetry_nerd/workspace/objects.py`,
  `src/telemetry_nerd/core/events.py`
- Test: `tests/unit/test_workspace_isolation.py`; adjust `tests/unit/test_event_log.py` only
  where `Event(...)` is constructed positionally.

**Interfaces:**
- Consumes: Task 1 columns; `ActiveWorkspace` is just a `Callable[[], str]` here.
- Produces:
  - `WorkspaceStore(db, clock=now_ms, scope=lambda: "w1", registry: WorkspaceRegistry | None
    = None)`; `ObjectStore(con, new_id, clock=now_ms, scope=lambda: "w1")`;
    `EventLog(con, clock=now_ms, scope=lambda: "w1")`.
  - `WrongWorkspace(ValueError)` in `model/errors.py`: message names the object, its
    workspace (and title when known) and says `workspace_switch to it first`.
  - `Event.workspace: str` (last field, default `"w1"`), in `to_dict()`.
  - `EventLog.tail(limit) -> list[Event]` (last `limit` events of the pinned workspace).
  - `ObjectStore.owner(obj_id) -> str | None`; `WorkspaceStore.owner(panel_id)`.
- Semantics (spec "Scoping rules"): inserts tag `scope()`; `list_*`, `since`, `tail`,
  `message_seqs` filter by it; `get_*` by id do not; `_update`, `set_spec`, `set_answered`,
  `close_panel` raise `WrongWorkspace` when the row's workspace is not `scope()`;
  `peek`, `claim`, `cursor`, `ack`, `seed_consumer`, `last_seq` stay global; `_fan_out` dicts
  carry `workspace`. `get_setting`/`set_setting` delegate to `registry` for `scope()` when a
  registry is given, else fall back to the old `workspace_settings` table (keeps
  `test_workspace_settings.py` green).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_workspace_isolation.py
import pytest

from telemetry_nerd.core.events import EventLog
from telemetry_nerd.model.errors import WrongWorkspace
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.models import AnnotationIn
from telemetry_nerd.workspace.objects import ObjectStore
from telemetry_nerd.workspace.scope import ActiveWorkspace
from telemetry_nerd.workspace.store import WorkspaceStore


@pytest.fixture
def stores(tmp_path):
    con = open_workspace_db(tmp_path / "workspace.db")
    active = ActiveWorkspace("w1")
    ws = WorkspaceStore(con, scope=active)
    return active, ws, ObjectStore(con, ws.next_id, scope=active), EventLog(con, scope=active)


def test_lists_are_disjoint_and_ids_global(stores):
    active, ws, objects, log = stores
    p1 = ws.create_panel("q1", {}, [])
    with active.using("w2"):
        p2 = ws.create_panel("q2", {}, [])
        h = objects.create_hypothesis("db is slow", "claude")
        assert [p.id for p in ws.list_panels()] == [p2.id]
    assert p1.id != p2.id
    assert [p.id for p in ws.list_panels()] == [p1.id]
    assert objects.list_hypotheses() == []
    assert objects.get_hypothesis(h.id).id == h.id  # get by id is global


def test_cross_workspace_update_is_refused(stores):
    active, ws, objects, _ = stores
    p = ws.create_panel("q1", {}, [])
    with active.using("w2"):
        with pytest.raises(WrongWorkspace, match="w1"):
            ws.close_panel(p.id)


def test_every_insert_records_its_workspace(stores):
    active, ws, objects, log = stores
    with active.using("w2"):
        objects.create_annotation(AnnotationIn(kind="note", label="x"), "user")
        log.append("user", "focus.changed", None, {"start_ms": 1, "end_ms": 2})
    con = ws.connection
    assert {r[0] for r in con.execute("SELECT workspace FROM objects")} == {"w2"}
    assert {r[0] for r in con.execute("SELECT workspace FROM events")} == {"w2"}


def test_since_filters_but_channel_peek_is_global(stores):
    active, _, _, log = stores
    log.append("user", "thread.message", None, {"thread": "t1", "text": "a", "message": "m1"})
    with active.using("w2"):
        log.append("user", "thread.message", None, {"thread": "t2", "text": "b", "message": "m2"})
        assert [e.workspace for e in log.since(0)] == ["w2"]
    intentional, _, _ = log.peek("claude")
    assert [e.workspace for e in intentional] == ["w1", "w2"]
    assert log.last_seq == 2
```

(Adapt `AnnotationIn` fields to the model; one more test per remaining list method:
annotations, findings, gaps, groups, code, threads, `message_seqs`, `tail`.)

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_workspace_isolation.py -v` → FAIL.
- [ ] **Step 3: Implement.** All SQL edits are in the three files. `ObjectStore._insert` adds
  `workspace`; `_list` adds `AND workspace = ?`; `_update` checks `owner()` first. `EventLog`:
  `INSERT` adds `workspace`; `_COLS` gains `workspace` (last, so `_event` stays positional);
  `since` filters; `peek` does not.
- [ ] **Step 4: Run** → PASS; full gates (existing tests use the default scope `w1`).
- [ ] **Step 5: Commit** `feat(workspace): scope panels, objects and events by workspace; ids stay global (ugr)`

---

### Task 4: Wire the scope and pin every request **[Opus]**

**Files:**
- Modify: `src/telemetry_nerd/core/bootstrap.py`, `tests/unit/fakes.py` (`make_service`),
  `src/telemetry_nerd/core/service.py` (`active`, `registry` fields),
  `src/telemetry_nerd/mcp/server.py` (`TelemetryMCP.call_tool`),
  `src/telemetry_nerd/api/app.py` (pin middleware)
- Test: `tests/unit/test_workspace_pinning.py`

**Interfaces:**
- Consumes: Tasks 2-3.
- Produces: `TelemetryService.active: ActiveWorkspace`, `TelemetryService.registry:
  WorkspaceRegistry` (both built in `build_service`/`make_service` from the same `wcon`;
  `ActiveWorkspace(registry.active_id())`); `PinWorkspace` pure ASGI middleware in
  `api/app.py` (pins `scope["type"] == "http"` only); `call_tool` runs inside
  `service.active.pinned()` (pass `service` into `TelemetryMCP`, or set
  `mcp.pin = service.active.pinned` in `build_mcp`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_workspace_pinning.py
import asyncio

from starlette.testclient import TestClient

from telemetry_nerd.api.app import create_app
from telemetry_nerd.mcp.server import build_mcp
from tests.unit.fakes import make_service

HOSTS = ["testserver"]


def test_make_service_shares_one_scope(tmp_path):
    svc = make_service(tmp_path)
    svc.registry.create("second", None)
    svc.active.set_active("w2")
    assert svc.ws.workspace.list_panels() == []  # reads follow the active id


async def test_sync_mcp_tool_sees_the_pin(tmp_path):
    svc = make_service(tmp_path)
    mcp = build_mcp(svc, "http://ui")
    svc.registry.create("second", None)
    svc.active.set_active("w2")
    await mcp.call_tool("annotate", {"kind": "note", "label": "here"})
    rows = svc.workspace.connection.execute("SELECT workspace FROM objects").fetchall()
    assert rows == [("w2",)]


async def test_in_flight_tool_finishes_in_its_workspace(tmp_path, monkeypatch):
    # a slow query: switch while it awaits the source; the dataset.created event lands in w1
    ...


def test_http_route_is_pinned(tmp_path):
    svc = make_service(tmp_path)
    with TestClient(create_app(svc, allowed_hosts=HOSTS)) as c:
        svc.registry.create("second", None)
        svc.active.set_active("w2")
        c.post("/api/annotations", json={"kind": "note", "label": "x"})
    rows = svc.workspace.connection.execute("SELECT workspace FROM objects").fetchall()
    assert rows == [("w2",)]
```

(The in-flight test: a `FakeSource` whose fetch awaits an `asyncio.Event`; start
`mcp.call_tool("query", ...)` as a task, `set_active("w2")`, release the event, assert the
`dataset.created` row's workspace is `w1`.)

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_workspace_pinning.py -v` → FAIL.
- [ ] **Step 3: Implement.** `get_default_range`/`set_default_range` now go through
  `WorkspaceStore` with its registry (Task 3), i.e. per workspace.
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `feat(workspace): one active-workspace scope per daemon, pinned per MCP call and HTTP request (ugr)`

---

### Task 5: Tier-2 runs across workspaces

**Files:**
- Modify: `src/telemetry_nerd/core/code_ops.py`
- Test: `tests/unit/test_code_ops.py` (add cases)

**Interfaces:**
- Consumes: `TelemetryService.active`, `registry.ids()`.
- Produces: `CodeOps(..., scope: Callable[[], str] = lambda: "w1", workspace_ids:
  Callable[[], list[str]] = lambda: ["w1"], using=...)`; `WORKSPACE_ID` removed;
  `kernels.execute(self.scope(), ...)`; `recover()` and `keep()` iterate `workspace_ids()`
  under `using(wid)`; module docstring "no node in any workspace".

- [ ] **Step 1: Write the failing tests**
  - `test_gc_keeps_run_dirs_of_other_workspaces`: a referenced code node in w1, switch the
    scope to w2, run GC: w1's run dir survives.
  - `test_kernel_keyed_by_active_workspace`: fake `KernelManager.execute` records
    `workspace_id`; a run under `using("w2")` passes `"w2"`.
  - `test_recover_fails_stale_runs_in_every_workspace`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_code_ops.py -v` → new cases FAIL.
- [ ] **Step 3: Implement**; pass the three callables from `TelemetryService.__post_init__`.
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `fix(code): kernels per active workspace; run-dir GC and recovery span all workspaces (ugr)`

---

### Task 6: WorkspaceOps: create, list, switch, update, restore sources

**Files:**
- Create: `src/telemetry_nerd/core/workspaces.py`
- Modify: `src/telemetry_nerd/core/service.py` (`workspaces: WorkspaceOps = field(init=False)`;
  `note_source` calls in `query` and `query_distribution`), `src/telemetry_nerd/core/events.py`
  (`workspace.opened` in `INTENTIONAL_TYPES`, `workspace.updated` in `AMBIENT_TYPES`)
- Test: `tests/unit/test_workspace_ops.py`

**Interfaces:**
- Consumes: registry, active, log, sources registry, `TelemetryService.source_connect` path.
- Produces (`WorkspaceOps`):
  - `create(title: str, question: str | None, actor: Actor) -> dict` (switches; the result of
    `switch` plus `created: True`).
  - `switch(wid: str, actor: Actor) -> dict`: `{workspace, previous, counts, open_threads,
    sources}`; no-op (no event) when already active; unarchives; appends
    `workspace.opened` under `using(wid)` with payload `{title, question, created, from}`;
    `registry.mark_opened`; `active.set_active`; `active.notify({"kind": "workspace",
    "active": info})`.
  - `update(wid, *, title=None, question=None, archived=None, actor) -> WorkspaceInfo`;
    refuses archiving the active one (`ValueError` with hint); event `workspace.updated`
    with the changed fields; notify.
  - `list(include_archived=False, limit=20) -> dict` `{active, workspaces, more}`.
  - `note_source(name)`; `async restore_sources(wid) -> list[dict]` (`connected | restored |
    conflict | failed`, never overrides an attached name, never records `default`).
  - `switch` is `async` (restore may connect); MCP/HTTP await it.

- [ ] **Step 1: Write the failing tests**
  - `test_create_switches_and_old_workspace_is_untouched` (panel in w1; create; snapshot
    empty; switch back; panel there; **3fs.1 acceptance at the service level**: panels,
    findings, hypotheses, threads, default_range all restored).
  - `test_user_switch_is_an_intentional_event_in_the_target_workspace` (actor user → klass
    intentional, `workspace == new`); `test_claude_switch_does_not_echo` (internal).
  - `test_switch_to_active_is_a_noop`.
  - `test_archive_active_is_refused`; `test_reopen_unarchives`.
  - `test_reopen_restores_a_disconnected_source` (fake factory; connect `vm`, query in w2,
    disconnect, switch to w1 and back to w2 → `restored`); `test_name_conflict_is_reported`.
  - `test_notify_reaches_subscribers`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_workspace_ops.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `feat(workspace): create, list, switch, rename and archive workspaces; reopening restores sources (ugr, 3fs.1)`

---

### Task 7: MCP tools

**Files:**
- Modify: `src/telemetry_nerd/mcp/server.py`, `src/telemetry_nerd/core/workspace_service.py`
  (`brief()` gains `workspace`, `activity()` uses `log.tail` when `since is None`)
- Test: `tests/unit/test_mcp_workspaces.py`; `tests/unit/test_mcp_instructions.py` (new bullet)

**Interfaces:**
- Produces tools `workspace_create(title: str, question: str | None = None)`,
  `workspace_list(include_archived: bool = False)`, `workspace_switch(id: str)`,
  `workspace_update(id: str, title: str | None = None, question: str | None = None,
  archived: bool | None = None)`; each returns `_dump(...)` of the `WorkspaceOps` result
  plus `url` (`ui_url`); errors via `_fail`. INSTRUCTIONS bullet per the spec. Tool calls
  inside `call_tool` are pinned, so after `workspace_switch` the *next* call sees the new
  workspace.

- [ ] **Step 1: Write the failing tests**: create → `workspace_get` shows the new id and no
  panels; list hides archived and caps at 20 with `more`; switch unknown id → ToolError with
  hint; every result `< 2048` bytes with 30 workspaces; `workspace_activity()` after 60 events
  in w1 and 3 in w2 returns 3 when w2 is active.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_mcp_workspaces.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `feat(mcp): workspace_create/list/switch/update; workspace_get names the active workspace (ugr)`

---

### Task 8: HTTP routes and UI socket

**Files:**
- Modify: `src/telemetry_nerd/api/app.py`
- Test: `tests/unit/test_api_workspaces.py`

**Interfaces:**
- Produces: `GET /api/workspaces[?archived=1]`, `POST /api/workspaces` `{title, question?}`,
  `POST /api/workspaces/{id}/open`, `POST /api/workspaces/{id}/update`
  `{title?, question?, archived?}` (actor `user`, `_api` error mapping, `_body` JSON checks);
  `GET /api/workspace` adds `workspace`; `/ws`: subscribe to `active.subscribe()` as a 4th
  awaited future, send `{"kind": "workspace", "active": ...}` frames, forward an event only
  when `event["workspace"] == active.active`, replay `since` through the scoped log
  (WebSocket scope is unpinned, so it reads the active id).

- [ ] **Step 1: Write the failing tests** (TestClient, like `test_api_workspace.py`):
  create via POST → `/api/workspace` is empty and names the new id; open w1 → panels back;
  `/ws` receives a `workspace` frame after `POST /api/workspaces/{id}/open`; an event
  appended in the inactive workspace is not forwarded; update with a blank title → 400;
  archive the active → 400.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_api_workspaces.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `feat(api): workspace routes; the UI socket follows the active workspace (ugr)`

---

### Task 9: Channel tag and event lines

**Files:**
- Modify: `src/telemetry_nerd/channel/format.py`, `src/telemetry_nerd/bridge/proxy.py`
  (`_instructions`), `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md` §7.2
  example stays valid (`w1` is a real id)
- Test: `tests/unit/test_channel_format.py`, `tests/unit/test_bridge_channel.py` (instructions
  text if asserted)

**Interfaces:**
- Produces: `meta["workspace"] = intentional[-1].workspace`; lines from another workspace
  prefixed `[<id>] `; `describe_event` cases `workspace.opened` (new vs reopened, question,
  previous) and `workspace.updated` (renamed / archived / unarchived / question changed).

- [ ] **Step 1: Write the failing tests**: `Event(..., workspace="w3")` → meta workspace `w3`;
  mixed batch (w1 ask, then w3 opened) → meta `w3`, first line starts `[w1] `; the two
  describe lines match the spec's wording.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_channel_format.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `feat(channel): channel events carry their real workspace; describe workspace switches (ugr)`

---

### Task 10: UI data layer

**Files:**
- Modify: `ui/src/lib/api.ts`, `ui/src/lib/workspace.svelte.ts`
- Create: `ui/src/lib/workspaces.ts`
- Test: `ui/src/lib/workspaces.test.ts`

**Interfaces:**
- Produces: `WorkspaceInfo` type (`id, title, question, archived, created_at_ms,
  last_activity_ms, counts`); `Snapshot.workspace: WorkspaceInfo`; `WorkspaceEvent.workspace`;
  `fetchWorkspaces(archived?)`, `createWorkspace(title, question?)`, `openWorkspace(id)`,
  `updateWorkspace(id, patch)`; `SocketHandlers.onWorkspace(frame)`; in `workspaces.ts`:
  `workspaceChanged(prev: Snapshot | null, next: Snapshot): boolean`,
  `defaultTitle(now: Date): string` ("Investigation 2026-10-03 14:05"),
  `sortForSwitcher(list, activeId)` (active first, then by activity). Store: `workspaces`
  getter; on `onWorkspace` or a changed snapshot: clear highlights, reload snapshot, refetch
  list.

- [ ] **Step 1: Write the failing vitest tests** for the three pure helpers.
- [ ] **Step 2: Run** `cd ui && npx vitest run src/lib/workspaces.test.ts` → FAIL.
- [ ] **Step 3: Implement**; keep `load()`'s `last_seq` guard (global seq, spec D5).
- [ ] **Step 4: Run** → PASS; gates (incl. `just ui-check`).
- [ ] **Step 5: Commit** `feat(ui): workspace data layer; the board reloads on a switch (ugr)`

---

### Task 11: Workspace switcher in the header

**Files:**
- Create: `ui/src/components/WorkspaceSwitcher.svelte`
- Modify: `ui/src/App.svelte` (header, empty-state text names the workspace title)
- Test: `ui/e2e/workspaces.spec.ts`

**Interfaces:**
- Consumes: Task 10 store and api functions.
- Produces: header button with the active title (`aria-haspopup="listbox"`); popover rows
  (title, last activity, finding count; question as `title=` tooltip) with Open, Rename
  (inline input, Enter saves, Esc cancels) and Archive (hidden on the active row); "New
  investigation" row with an inline title input (empty → `defaultTitle`); "Show archived"
  toggle. Svelte 5 runes, styles from `index.css` tokens, no new dependency.

- [ ] **Step 1: Write the failing e2e test**: seed a panel; New investigation "second" →
  board empty, header shows "second"; open the first → the panel is back; a second browser
  page follows the switch without reload; archive "second" → hidden until "Show archived".
- [ ] **Step 2: Run** `just ui-build && cd ui && npx playwright test e2e/workspaces.spec.ts`
  (needs `just dev-up`) → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; gates.
- [ ] **Step 5: Commit** `feat(ui): workspace switcher: new investigation, reopen, rename, archive (ugr, 3fs.1)`

---

### Task 12: Docs and plugin commands

**Files:**
- Modify: `docs/superpowers/specs/2026-09-30-telemetry-nerd-mvp-design.md` (§2.1 one file,
  workspace column; §3.3 Workspace row: title, question, focus, sources, archived; §7.1
  Collaboration row adds the four tools; §7.2 tag is the real id),
  `commands/investigate.md` (step 1: if the active workspace holds a different
  investigation, `workspace_create(title, question)` and say so in one line; the old one stays
  reopenable), `commands/open.md` (brief names the workspace; mention the switcher),
  `commands/start.md` (step 6 next steps: `workspace_list` to resume an old investigation),
  `skills/triage/SKILL.md` (orient line), `docs/principles.md` principle 7 "Enforced by"
  adds `workspace/scope.py`, `workspace/registry.py`
- Add `workspace_create`, `workspace_list`, `workspace_switch` to `allowed-tools` of
  `investigate.md` and `open.md`.
- Test: `uv run pytest tests/unit/test_commands.py -q` (command frontmatter checks).

- [ ] Steps: edit, run the command tests and gates, commit
  `docs: workspace management in the MVP spec, commands and triage skill (ugr)`.

---

### Task 13: Simplify pass and close-out

- [ ] Run `/simplify` over the branch diff (per the epic close criteria); fix what it finds.
- [ ] Full gates plus `just e2e`.
- [ ] File follow-up beads from the spec's "Follow-ups"; close telemetry-nerd-ugr and
  telemetry-nerd-3fs.1 with the acceptance evidence (Task 6 service test, Task 11 e2e).
- [ ] Commit `refactor(workspace): simplify after workspace management (ugr)`.

---

## Order and dependencies

```
1 ─▶ 2 ─▶ 3 ─▶ 4 ─┬─▶ 5
                  └─▶ 6 ─┬─▶ 7
                         ├─▶ 8 ─▶ 10 ─▶ 11
                         └─▶ 9
                                   12, 13 last
```

Tasks 5, 7, 8, 9 are independent of each other once 6 lands (5 needs only 4).
