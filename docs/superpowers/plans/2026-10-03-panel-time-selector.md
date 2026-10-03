# Panel Time-Frame Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user change a panel's time range from the UI (presets, typed `now-N`, absolute
from/to) with instant zoom for in-bounds changes and a safe, evidence-preserving path for
changes that need new data.

**Architecture:** Two-tier range change. Tier 1 (viewport) is a pure client-side resample when
the requested range is already inside the panel's fetched buckets — no backend call. Tier 2
(preview) fetches a new dataset via a new `preview()` service method when the range exceeds
what's fetched, rendered with a "previewing" badge but never persisted. The preview only
becomes durable when the user clicks "keep this range", which calls a new `rescope()` service
method that mirrors the existing `reframe()` (`src/telemetry_nerd/core/service.py:943`): it
creates a **new panel** over the new range, leaving the old panel and everything anchored to
it untouched. A separate, independent piece adds a workspace-level default time range read by
`query()` when no start/end is given.

**Tech Stack:** Python (TelemetryService / WorkspaceStore / SQLite), Starlette HTTP routes,
Svelte 5 + TypeScript frontend, pytest, Playwright e2e.

**Spec:** `docs/superpowers/specs/2026-10-03-panel-time-selector-design.md`

## Global Constraints

- `rescope()` never mutates an existing panel; it always produces a new panel id (per the
  spec's "why not in-place mutation" section — this is the one invariant the whole design
  rests on).
- `preview()` must not write to the `panels` table and must not log any ambient/intentional
  event (only the routine internal `dataset.created` event that any `query()` call logs).
- v1 `rescope()` supports `line+envelope`, `fleet`, and `spc` panels only. Any other mark
  raises `ValueError` with message prefix `rescope_unsupported_for_mark`.
- No frontend wiring of automatic crystallize-on-annotate in v1 — only an explicit "keep this
  range" button. The selection menu's ask/annotate actions are disabled while a preview is
  active and not yet kept.

---

## File Structure

Backend:
- `src/telemetry_nerd/workspace/db.py` — new `workspace_settings` table.
- `src/telemetry_nerd/workspace/store.py` — `WorkspaceStore.get_setting`/`set_setting`.
- `src/telemetry_nerd/core/events.py` — new ambient event type `panel.rescoped`.
- `src/telemetry_nerd/channel/format.py` — human-readable line for `panel.rescoped`.
- `src/telemetry_nerd/core/service.py` — `preview()`, `rescope()` methods; `query()` reads the
  workspace default range; `get_default_range()`/`set_default_range()`.
- `src/telemetry_nerd/api/app.py` — routes for preview, rescope, default-range get/set.

Frontend:
- `ui/src/chart/viewport.ts` (new) — viewport state, in-bounds check.
- `ui/src/lib/api.ts` — `previewPanel`, `rescopePanel`, `fetchDefaultRange`, `setDefaultRange`.
- `ui/src/Panel.svelte` — range control UI, preview badge, keep-this-range button.

Tests:
- `tests/unit/test_workspace_settings.py` (new).
- `tests/unit/test_time_selector_service.py` (new) — `preview()`/`rescope()`.
- `ui/src/chart/viewport.test.ts` (new).
- `ui/e2e/time-selector.spec.ts` (new).

---

### Task 1: Workspace settings storage

**Files:**
- Modify: `src/telemetry_nerd/workspace/db.py` (add table to `_SCHEMA`)
- Modify: `src/telemetry_nerd/workspace/store.py` (add two methods to `WorkspaceStore`)
- Test: `tests/unit/test_workspace_settings.py`

**Interfaces:**
- Produces: `WorkspaceStore.get_setting(key: str, default: str | None = None) -> str | None`,
  `WorkspaceStore.set_setting(key: str, value: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_workspace_settings.py
from telemetry_nerd.workspace.db import open_workspace_db
from telemetry_nerd.workspace.store import WorkspaceStore


def test_get_setting_returns_default_when_unset(tmp_path):
    store = WorkspaceStore(open_workspace_db(tmp_path / "workspace.db"))
    assert store.get_setting("default_range") is None
    assert store.get_setting("default_range", "now-1h") == "now-1h"


def test_set_setting_then_get_returns_the_new_value(tmp_path):
    store = WorkspaceStore(open_workspace_db(tmp_path / "workspace.db"))
    store.set_setting("default_range", "now-3h")
    assert store.get_setting("default_range") == "now-3h"


def test_set_setting_overwrites_an_existing_value(tmp_path):
    store = WorkspaceStore(open_workspace_db(tmp_path / "workspace.db"))
    store.set_setting("default_range", "now-3h")
    store.set_setting("default_range", "now-6h")
    assert store.get_setting("default_range") == "now-6h"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_workspace_settings.py -v`
Expected: FAIL with `sqlite3.OperationalError: no such table: workspace_settings` (or
`AttributeError: 'WorkspaceStore' object has no attribute 'get_setting'`)

- [ ] **Step 3: Add the table**

In `src/telemetry_nerd/workspace/db.py`, add to `_SCHEMA` (after the `sources` table, before
the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS workspace_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

- [ ] **Step 4: Add the store methods**

In `src/telemetry_nerd/workspace/store.py`, add to `WorkspaceStore` (near `next_id`):

```python
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute(
            "SELECT value FROM workspace_settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO workspace_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_workspace_settings.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/workspace/db.py src/telemetry_nerd/workspace/store.py tests/unit/test_workspace_settings.py
git commit -m "feat(workspace): key/value settings storage for workspace-level config"
```

---

### Task 2: Workspace-level default time range

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (`query()` signature + `get_default_range`/`set_default_range`)
- Modify: `src/telemetry_nerd/api/app.py` (two new routes)
- Test: `tests/unit/test_time_selector_service.py`

**Interfaces:**
- Consumes: `WorkspaceStore.get_setting`/`set_setting` from Task 1.
- Produces: `TelemetryService.get_default_range() -> str`,
  `TelemetryService.set_default_range(value: str) -> str` (raises `ValueError` if `value`
  doesn't parse as a time expression). `query()`'s `start`/`end` defaults change from the
  literal strings `"now-1h"`/`"now"` to `None`, resolved inside the method.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_time_selector_service.py
import pytest

from tests.unit.fakes import make_service


async def test_query_falls_back_to_now_1h_when_no_default_set(tmp_path):
    svc = make_service(tmp_path)
    out = await svc.query("rate(node_cpu_seconds_total[5m])")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3600_000, rel=0.05)


async def test_query_uses_the_workspace_default_range_when_set(tmp_path):
    svc = make_service(tmp_path)
    svc.set_default_range("now-3h")
    out = await svc.query("rate(node_cpu_seconds_total[5m])")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3 * 3600_000, rel=0.05)


async def test_explicit_start_overrides_the_workspace_default(tmp_path):
    svc = make_service(tmp_path)
    svc.set_default_range("now-3h")
    out = await svc.query("rate(node_cpu_seconds_total[5m])", start="now-1h")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3600_000, rel=0.05)


def test_set_default_range_rejects_an_unparseable_value(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ValueError):
        svc.set_default_range("not a time")


def test_get_default_range_is_now_1h_before_anything_is_set(tmp_path):
    svc = make_service(tmp_path)
    assert svc.get_default_range() == "now-1h"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_time_selector_service.py -v`
Expected: FAIL with `AttributeError: 'TelemetryService' object has no attribute 'set_default_range'`

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/core/service.py`, change the `query()` signature (around line 394-399)
from:

```python
    async def query(
        self,
        expr: str,
        start: str = "now-1h",
        end: str = "now",
```

to:

```python
    async def query(
        self,
        expr: str,
        start: str | None = None,
        end: str | None = None,
```

and as the first line of the method body (right after the `is_code_expr` check, before
`src = self._source(source)`):

```python
        start = start or self.get_default_range()
        end = end or "now"
```

Add two new methods near `_time_summary` (same class, `TelemetryService`):

```python
    def get_default_range(self) -> str:
        """The `start` new panels default to when the caller doesn't say (bead aqk)."""
        return self.workspace.get_setting("default_range", "now-1h")

    def set_default_range(self, value: str) -> str:
        parse_time(value, self.clock())  # raises ValueError if unparseable
        self.workspace.set_setting("default_range", value)
        return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_time_selector_service.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Add the HTTP routes**

In `src/telemetry_nerd/api/app.py`, add two handlers near `panel_y_context` (around line 438):

```python
    @_api
    async def workspace_default_range(request: Request) -> object:
        return {"default_range": service.get_default_range()}

    @_api
    async def set_workspace_default_range(request: Request) -> object:
        body = await _body(request, default_range=str)
        return {"default_range": service.set_default_range(body["default_range"])}
```

Register both in the `Route(...)` list (near `/api/panels/{id}/reframe`, around line 841):

```python
        Route("/api/workspace/default-range", workspace_default_range, methods=["GET"]),
        Route("/api/workspace/default-range", set_workspace_default_range, methods=["POST"]),
```

- [ ] **Step 6: Run the full unit test suite to check for regressions**

Run: `uv run pytest tests/unit -x -q`
Expected: PASS (no regressions — `query()`'s externally-observed default behavior is
unchanged when no workspace setting has been set)

- [ ] **Step 7: Commit**

```bash
git add src/telemetry_nerd/core/service.py src/telemetry_nerd/api/app.py tests/unit/test_time_selector_service.py
git commit -m "feat(service): workspace-level default time range for new panels"
```

---

### Task 3: `panel.rescoped` event type

**Files:**
- Modify: `src/telemetry_nerd/core/events.py` (`AMBIENT_TYPES`)
- Modify: `src/telemetry_nerd/channel/format.py` (`describe_event`)
- Test: `tests/unit/test_events.py` (check if this file exists first; if not, add the test to
  `tests/unit/test_time_selector_service.py` from Task 2)

**Interfaces:**
- Produces: `"panel.rescoped"` is now a valid ambient event type; `describe_event` renders it.

- [ ] **Step 1: Check whether `tests/unit/test_events.py` exists**

Run: `ls tests/unit/test_events.py`

If it exists, add the new tests there; otherwise append to
`tests/unit/test_time_selector_service.py`.

- [ ] **Step 2: Write the failing test**

```python
from telemetry_nerd.channel.format import describe_event
from telemetry_nerd.core.events import Event, classify


def test_panel_rescoped_is_ambient_for_a_user_actor():
    assert classify("user", "panel.rescoped", {"from": "p1"}) == "ambient"


def test_panel_rescoped_is_internal_for_a_claude_actor():
    assert classify("claude", "panel.rescoped", {"from": "p1"}) == "internal"


def test_describe_event_renders_panel_rescoped():
    e = Event(1, 0, "user", "panel.rescoped", "p2", "ambient", {"from": "p1"})
    assert describe_event(e) == "user rescoped p1 to p2"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k rescoped -v`
Expected: FAIL (`classify` returns `"internal"` for the first test; `describe_event` raises on
an unmatched `case`)

- [ ] **Step 4: Add the event type**

In `src/telemetry_nerd/core/events.py`, add `"panel.rescoped"` to the `AMBIENT_TYPES` set
(alongside `"panel.y_view_selected"` etc.).

- [ ] **Step 5: Add the format case**

In `src/telemetry_nerd/channel/format.py`, add a case to `describe_event` right after
`"panel.closed"`:

```python
        case "panel.rescoped":
            return f"{who} rescoped {p['from']} to {e.object_id}"
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k rescoped -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add src/telemetry_nerd/core/events.py src/telemetry_nerd/channel/format.py tests/unit/test_time_selector_service.py
git commit -m "feat(events): panel.rescoped ambient event type"
```

---

### Task 4: `preview()` service method

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (new method, near `reframe()`)
- Test: `tests/unit/test_time_selector_service.py`

**Interfaces:**
- Consumes: `self.workspace.get_panel`, `self.datasets.meta`, `refuse_requery` (already
  imported — used by `reframe()`), `self.query` (Task 2's version).
- Produces: `async def preview(self, panel_id: str, start: str, end: str, actor: Actor = "user") -> dict`
  — same return shape as `query()`: `{"dataset": <id>, "summary": {...}}`.

- [ ] **Step 1: Write the failing test**

```python
async def test_preview_returns_a_dataset_over_the_new_range_without_touching_the_panel(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    pid = shown.panel.id
    before = svc.workspace.get_panel(pid)
    out = await svc.preview(pid, "now-3h", "now")
    meta = svc.datasets.meta(out["dataset"])
    assert meta.end_ms - meta.start_ms == pytest.approx(3 * 3600_000, rel=0.05)
    after = svc.workspace.get_panel(pid)
    assert after == before  # untouched: same question, status, spec, dataset_ids


async def test_preview_logs_no_ambient_or_intentional_event(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    before_seq = svc.log.last_seq
    await svc.preview(shown.panel.id, "now-3h", "now")
    new_events = svc.log.since(before_seq)
    assert all(e.klass == "internal" for e in new_events)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k preview -v`
Expected: FAIL with `AttributeError: 'TelemetryService' object has no attribute 'preview'`

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/core/service.py`, add right before `async def reframe`:

```python
    async def preview(
        self, panel_id: str, start: str, end: str, actor: Actor = "user"
    ) -> dict:
        """A dataset over a different range for `panel_id`, without touching it (bead aqk):
        the server side of a client-side zoom preview. Nothing is persisted or logged beyond
        the routine internal dataset.created event any fetch makes."""
        p = self.workspace.get_panel(panel_id)
        meta = self.datasets.meta(p.dataset_ids[0])
        refuse_requery(meta, "a time-range preview")
        return await self.query(
            meta.expr, start=start, end=end, step=format_duration(meta.step_ms),
            source=meta.source, actor=actor,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k preview -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/core/service.py tests/unit/test_time_selector_service.py
git commit -m "feat(service): preview() fetches a panel's data over a new range without mutating it"
```

---

### Task 5: `rescope()` for `line+envelope` panels

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (new method, right after `preview()`)
- Test: `tests/unit/test_time_selector_service.py`

**Interfaces:**
- Consumes: `preview()`-adjacent pieces (`refuse_requery`, `self.query`), `AutoForm` (already
  imported, used by `reframe()`), `self.show`, `self.log.append`.
- Produces: `async def rescope(self, panel_id: str, start: str, end: str, actor: Actor = "user") -> ShowResult`.
  Raises `ValueError("rescope_unsupported_for_mark: ...")` for marks other than
  `line+envelope`, `fleet`, `spc` (fleet and spc added in Tasks 6-7; this task only handles the
  `line+envelope` branch and the unsupported-mark branch).

- [ ] **Step 1: Write the failing test**

```python
async def test_rescope_makes_a_new_panel_and_leaves_the_old_one_untouched(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    pid = shown.panel.id
    before = svc.workspace.get_panel(pid)
    res = await svc.rescope(pid, "now-3h", "now", "user")
    assert res.panel.id != pid
    assert svc.workspace.get_panel(pid) == before
    meta = svc.datasets.meta(res.panel.dataset_ids[0])
    assert meta.end_ms - meta.start_ms == pytest.approx(3 * 3600_000, rel=0.05)
    assert res.panel.spec["auto"]["transform"] == "rescope"
    assert res.panel.spec["auto"]["source_dataset"] == before.dataset_ids[0]
    assert "rescoped from" in res.panel.spec["auto"]["reason"]


async def test_rescope_logs_a_panel_rescoped_event(tmp_path):
    svc = make_service(tmp_path)
    shown = svc.show((await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"], "cpu?")
    pid = shown.panel.id
    before_seq = svc.log.last_seq
    res = await svc.rescope(pid, "now-3h", "now", "user")
    [e] = [e for e in svc.log.since(before_seq) if e.type == "panel.rescoped"]
    assert e.object_id == res.panel.id and e.payload["from"] == pid


async def test_rescope_refuses_a_code_output_panel(tmp_path):
    import pyarrow as pa

    from telemetry_nerd.datasets.store import Lineage
    from telemetry_nerd.model.series import BUCKET_SCHEMA, SERIES_SCHEMA, FetchResult
    from telemetry_nerd.model.time import TimeRange

    svc = make_service(tmp_path)
    # a code output has no source expr to re-query (same refusal reframe() uses)
    code_ds = svc.datasets.put(
        source="default", expr="code", rng=TimeRange(0, 1000), step_ms=1000, resolution_ms=1000,
        result=FetchResult(pa.table({}, schema=BUCKET_SCHEMA), pa.table({}, schema=SERIES_SCHEMA)),
        lineage=Lineage(producer={"kind": "code", "node": "n1", "output": "o1"}),
    )
    code_panel = svc.show(code_ds.id, "computed?")
    with pytest.raises(ValueError, match="fixed data"):
        await svc.rescope(code_panel.panel.id, "now-3h", "now", "user")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k rescope -v`
Expected: FAIL with `AttributeError: 'TelemetryService' object has no attribute 'rescope'`

- [ ] **Step 3: Implement**

In `src/telemetry_nerd/core/service.py`, add right after `preview()`:

```python
    async def rescope(
        self, panel_id: str, start: str, end: str, actor: Actor = "user"
    ) -> ShowResult:
        """Accept a time-range change (bead aqk): a NEW panel over the new range, marked as
        rescoped from this one, which is left exactly as it was. Never applied silently."""
        p = self.workspace.get_panel(panel_id)
        spec = ChartSpec.model_validate(p.spec)
        mark = spec.layers[0].mark if spec.layers else "auto"
        if mark not in ("line+envelope", "fleet", "spc"):
            raise ValueError(
                f"rescope_unsupported_for_mark: {panel_id} is a {mark} panel; rescope only "
                "supports line+envelope, fleet and spc panels for now (hint: create a new "
                "panel over the new range instead)"
            )
        meta = self.datasets.meta(p.dataset_ids[0])
        refuse_requery(meta, "a time-range rescope")
        ds = (
            await self.query(
                meta.expr, start=start, end=end, step=format_duration(meta.step_ms),
                source=meta.source, actor=actor,
            )
        )["dataset"]
        form = AutoForm(
            transform="rescope", source_dataset=p.dataset_ids[0],
            reason=f"rescoped from {panel_id}",
        )
        kwargs: dict = {}
        res = self.show(ds, p.question, actor, auto=form, raw_ok=True, **kwargs)
        self.log.append(actor, "panel.rescoped", res.panel.id, {"from": panel_id})
        return res
```

(The `kwargs` dict is empty for `line+envelope` — `show()` with no `mark` kwarg defaults to
`"auto"`, reconstructing a plain line panel the same way a fresh `show()` call would. Tasks 6
and 7 add the `fleet`/`spc` branches that populate it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k rescope -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/telemetry_nerd/core/service.py tests/unit/test_time_selector_service.py
git commit -m "feat(service): rescope() for line+envelope panels, mirroring reframe()"
```

---

### Task 6: `rescope()` for `fleet` panels

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (`rescope()` method from Task 5)
- Test: `tests/unit/test_time_selector_service.py`

**Interfaces:**
- Consumes: `self.fleets.last_config(dataset_id) -> dict`, `self.fleets.summary(dataset_id, **cfg) -> dict`
  (both already exist in `FleetOps`, used by the `fleet` MCP tool and by `show(mark="fleet")`).

- [ ] **Step 1: Write the failing test**

```python
from tests.unit.fakes import FakeSource  # add to the file's existing import block


async def test_rescope_a_fleet_panel_carries_over_its_options(tmp_path):
    svc = make_service(tmp_path, source=FakeSource(n_series=6))
    d = (await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"]
    shown = svc.show(d, "per-core?", mark="fleet", bounds_lo=0, bounds_hi=100)
    pid = shown.panel.id
    res = await svc.rescope(pid, "now-3h", "now", "user")
    assert res.panel.id != pid
    assert res.panel.spec["layers"][0]["mark"] == "fleet"
    new_cfg = svc.fleets.last_config(res.panel.dataset_ids[0])
    old_cfg = svc.fleets.last_config(d)
    assert new_cfg == old_cfg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k fleet_panel_carries -v`
Expected: FAIL — the new panel comes back as a `line+envelope` panel (or raises), not `fleet`,
because `kwargs` is still empty for the `fleet` mark.

- [ ] **Step 3: Implement**

In `rescope()`, replace the `kwargs: dict = {}` line and the lines immediately below it with:

```python
        kwargs: dict = {}
        if mark == "fleet":
            old_cfg = self.fleets.last_config(p.dataset_ids[0])
            self.fleets.summary(ds, **old_cfg)
            kwargs["mark"] = "fleet"
```

(leave the `res = self.show(...)` line as-is, right after this block)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k fleet_panel_carries -v`
Expected: PASS

- [ ] **Step 5: Run the full rescope test group to check for regressions**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k rescope -v`
Expected: PASS (all previous + new test)

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/core/service.py tests/unit/test_time_selector_service.py
git commit -m "feat(service): rescope() re-derives fleet config on the new dataset"
```

---

### Task 7: `rescope()` for `spc` panels

**Files:**
- Modify: `src/telemetry_nerd/core/service.py` (`rescope()` method)
- Test: `tests/unit/test_time_selector_service.py`

**Interfaces:**
- Consumes: `spec.layers[0].windows` (list of `Window`, already imported), `Window` class
  (already imported in `service.py` — used by `show()`'s own `spc` branch).

- [ ] **Step 1: Write the failing test**

```python
from telemetry_nerd.charts.spec import Window


async def test_rescope_an_spc_panel_reuses_its_baseline_when_it_still_fits(tmp_path):
    svc = make_service(tmp_path)
    d = (await svc.query("rate(node_cpu_seconds_total[5m])", start="now-6h", end="now"))["dataset"]
    meta = svc.datasets.meta(d)
    baseline_start, baseline_end = meta.start_ms, meta.start_ms + meta.step_ms * 3
    shown = svc.show(
        d, "spc?", mark="spc",
        windows=[Window(start_ms=baseline_start, end_ms=baseline_end)],
    )
    pid = shown.panel.id
    res = await svc.rescope(pid, "now-6h", "now", "user")
    assert res.panel.spec["layers"][0]["mark"] == "spc"
    w = res.panel.spec["layers"][0]["windows"][0]
    assert w["start_ms"] == baseline_start and w["end_ms"] == baseline_end


async def test_rescope_an_spc_panel_raises_when_the_baseline_no_longer_fits(tmp_path):
    svc = make_service(tmp_path)
    d = (await svc.query("rate(node_cpu_seconds_total[5m])", start="now-6h", end="now"))["dataset"]
    meta = svc.datasets.meta(d)
    baseline_start, baseline_end = meta.start_ms, meta.start_ms + meta.step_ms * 3
    shown = svc.show(
        d, "spc?", mark="spc",
        windows=[Window(start_ms=baseline_start, end_ms=baseline_end)],
    )
    pid = shown.panel.id
    with pytest.raises(ValueError, match="baseline"):
        await svc.rescope(pid, "now-10m", "now", "user")  # new range excludes the old baseline
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k spc_panel -v`
Expected: FAIL — `rescope` raises `rescope_unsupported_for_mark` for both (the `spc` branch
doesn't exist in `kwargs` construction yet).

- [ ] **Step 3: Implement**

In `rescope()`, extend the `kwargs` block (right after the `fleet` branch added in Task 6):

```python
        elif mark == "spc":
            old_windows = spec.layers[0].windows
            kwargs["mark"] = "spc"
            if old_windows:
                w = old_windows[0]
                kwargs["windows"] = [Window(start_ms=w.start_ms, end_ms=w.end_ms)]
```

(`show()`'s own `spc` branch already calls `resolve_baseline(meta, w.start_ms, w.end_ms)`
against the *new* dataset's `meta` and raises if it doesn't fit — no extra validation needed
here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_time_selector_service.py -k spc_panel -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Add the unsupported-mark test and run the full rescope group**

```python
async def test_rescope_a_seasonal_panel_raises_unsupported(tmp_path):
    svc = make_service(tmp_path)
    d = (await svc.query("rate(node_cpu_seconds_total[5m])"))["dataset"]
    await svc.compare_seasonal(d, cycles=["day"])
    pid = svc.show(d, "seasonal?", mark="seasonal").panel.id
    with pytest.raises(ValueError, match="rescope_unsupported_for_mark"):
        await svc.rescope(pid, "now-3h", "now", "user")
```

Run: `uv run pytest tests/unit/test_time_selector_service.py -k rescope -v`
Expected: PASS (every rescope test in the file)

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/core/service.py tests/unit/test_time_selector_service.py
git commit -m "feat(service): rescope() reuses spc baseline when it still fits the new range"
```

---

### Task 8: HTTP routes for preview and rescope

**Files:**
- Modify: `src/telemetry_nerd/api/app.py`
- Test: `tests/unit/test_api.py` (append)

**Interfaces:**
- Consumes: `service.preview`, `service.rescope` from Tasks 4-7; the existing `client` fixture
  (`tests/unit/test_api.py:18`, a Starlette `TestClient`, synchronous — no `await` on its
  calls) and `make_panel(client, question=...)` helper (`tests/unit/test_api.py:27`), which
  POSTs `/api/query` then `/api/show` and returns the `show` response.
- Produces: `POST /api/panels/{id}/preview` → `{"dataset": ..., "summary": {...}}`;
  `POST /api/panels/{id}/rescope` → `{"panel": {...}, "issues": [...]}` (same shape
  `make_panel`'s response already has).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_api.py`:

```python
def test_panel_preview_returns_a_dataset_without_mutating_the_panel(client):
    pid = make_panel(client).json()["panel"]["id"]
    resp = client.post(f"/api/panels/{pid}/preview", json={"start": "now-3h", "end": "now"})
    assert resp.status_code == 200
    assert "dataset" in resp.json()


def test_panel_rescope_returns_a_new_panel(client):
    pid = make_panel(client).json()["panel"]["id"]
    resp = client.post(f"/api/panels/{pid}/rescope", json={"start": "now-3h", "end": "now"})
    assert resp.status_code == 200
    assert resp.json()["panel"]["id"] != pid


def test_panel_preview_404s_on_an_unknown_panel(client):
    resp = client.post("/api/panels/p999/preview", json={"start": "now-3h", "end": "now"})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_api.py -k "panel_preview or panel_rescope" -v`
Expected: FAIL with 404 (route not found) for all three

- [ ] **Step 3: Implement the routes**

In `src/telemetry_nerd/api/app.py`, add near `panel_reframe` (around line 428):

```python
    @_api
    async def panel_preview(request: Request) -> object:
        body = await _body(request, start=str, end=str)
        try:
            return await service.preview(request.path_params["id"], body["start"], body["end"], "user")
        except SourceError as e:
            raise _BadRequest(str(e), e.hint or "") from e

    @_api
    async def panel_rescope(request: Request) -> object:
        body = await _body(request, start=str, end=str)
        try:
            res = await service.rescope(request.path_params["id"], body["start"], body["end"], "user")
        except SourceError as e:
            raise _BadRequest(str(e), e.hint or "") from e
        return {"panel": res.panel.to_dict(), "issues": [i.model_dump() for i in res.issues]}
```

Register both, next to `/api/panels/{id}/reframe` in the `Route(...)` list:

```python
        Route("/api/panels/{id}/preview", panel_preview, methods=["POST"]),
        Route("/api/panels/{id}/rescope", panel_rescope, methods=["POST"]),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_api.py -k "panel_preview or panel_rescope" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full API test file to check for regressions**

Run: `uv run pytest tests/unit/test_api.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/telemetry_nerd/api/app.py tests/unit/test_api.py
git commit -m "feat(api): routes for panel time-range preview and rescope"
```

---

### Task 9: Frontend viewport model

**Files:**
- Create: `ui/src/chart/viewport.ts`
- Test: `ui/src/chart/viewport.test.ts`

**Interfaces:**
- Produces:
  ```typescript
  export interface Viewport { start_ms: number; end_ms: number; }
  export function isInBounds(viewport: Viewport, fetched: { start_ms: number; end_ms: number }): boolean;
  export function presetRange(preset: "15m" | "1h" | "6h" | "24h" | "7d", now_ms: number): Viewport;
  export function parseRelative(text: string, now_ms: number): Viewport | null; // "now-3h" style; null if unparseable
  ```

- [ ] **Step 1: Check the existing `yview.ts` test file's structure to match style**

Run: `rg -n "^import|^describe|^test" ui/src/chart/yview.test.ts | head -10`

- [ ] **Step 2: Write the failing test**

```typescript
// ui/src/chart/viewport.test.ts
import { describe, expect, test } from "vitest";
import { isInBounds, parseRelative, presetRange } from "./viewport";

describe("isInBounds", () => {
  test("true when the viewport is fully inside the fetched range", () => {
    const fetched = { start_ms: 0, end_ms: 10_000 };
    expect(isInBounds({ start_ms: 1000, end_ms: 9000 }, fetched)).toBe(true);
  });

  test("false when the viewport extends before the fetched start", () => {
    const fetched = { start_ms: 1000, end_ms: 10_000 };
    expect(isInBounds({ start_ms: 0, end_ms: 9000 }, fetched)).toBe(false);
  });

  test("false when the viewport extends past the fetched end", () => {
    const fetched = { start_ms: 0, end_ms: 9000 };
    expect(isInBounds({ start_ms: 1000, end_ms: 10_000 }, fetched)).toBe(false);
  });
});

describe("presetRange", () => {
  test("1h preset is exactly one hour ending now", () => {
    const now = 10_000_000;
    const v = presetRange("1h", now);
    expect(v.end_ms).toBe(now);
    expect(now - v.start_ms).toBe(3600_000);
  });
});

describe("parseRelative", () => {
  test("parses now-3h relative to now_ms", () => {
    const now = 10_000_000;
    expect(parseRelative("now-3h", now)).toEqual({ start_ms: now - 3 * 3600_000, end_ms: now });
  });

  test("returns null for unparseable text", () => {
    expect(parseRelative("not a range", 0)).toBeNull();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ui && npx vitest run src/chart/viewport.test.ts`
Expected: FAIL (module `./viewport` does not exist)

- [ ] **Step 4: Implement**

```typescript
// ui/src/chart/viewport.ts
export interface Viewport {
  start_ms: number;
  end_ms: number;
}

export const isInBounds = (
  viewport: Viewport,
  fetched: { start_ms: number; end_ms: number },
): boolean => viewport.start_ms >= fetched.start_ms && viewport.end_ms <= fetched.end_ms;

const PRESET_MS: Record<string, number> = {
  "15m": 15 * 60_000,
  "1h": 3600_000,
  "6h": 6 * 3600_000,
  "24h": 24 * 3600_000,
  "7d": 7 * 24 * 3600_000,
};

export const presetRange = (preset: keyof typeof PRESET_MS, now_ms: number): Viewport => ({
  start_ms: now_ms - PRESET_MS[preset],
  end_ms: now_ms,
});

const RELATIVE = /^now-(\d+)(s|m|h|d)$/;
const UNIT_MS: Record<string, number> = { s: 1000, m: 60_000, h: 3600_000, d: 86_400_000 };

export const parseRelative = (text: string, now_ms: number): Viewport | null => {
  const m = RELATIVE.exec(text.trim());
  if (!m) return null;
  return { start_ms: now_ms - Number(m[1]) * UNIT_MS[m[2]], end_ms: now_ms };
};
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ui && npx vitest run src/chart/viewport.test.ts`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add ui/src/chart/viewport.ts ui/src/chart/viewport.test.ts
git commit -m "feat(ui): viewport model for panel time-range zoom"
```

---

### Task 10: `api.ts` client calls

**Files:**
- Modify: `ui/src/lib/api.ts`

**Interfaces:**
- Consumes: `postJSON` (existing helper, `ui/src/lib/api.ts:328`).
- Produces:
  ```typescript
  export const previewPanel: (id: string, start: string, end: string) => Promise<{ dataset: string; summary: unknown }>;
  export const rescopePanel: (id: string, start: string, end: string) => Promise<{ panel: Panel }>;
  export const fetchDefaultRange: () => Promise<{ default_range: string }>;
  export const setDefaultRange: (value: string) => Promise<{ default_range: string }>;
  ```

- [ ] **Step 1: Add the functions**

In `ui/src/lib/api.ts`, near `reframePanel` (line 397):

```typescript
export const previewPanel = (id: string, start: string, end: string) =>
  postJSON<{ dataset: string; summary: unknown }>(`/api/panels/${id}/preview`, { start, end });
export const rescopePanel = (id: string, start: string, end: string) =>
  postJSON<{ panel: Panel }>(`/api/panels/${id}/rescope`, { start, end });
export const fetchDefaultRange = () =>
  fetch("/api/workspace/default-range").then((r) => json<{ default_range: string }>(r));
export const setDefaultRange = (default_range: string) =>
  postJSON<{ default_range: string }>("/api/workspace/default-range", { default_range });
```

- [ ] **Step 2: Type-check**

Run: `cd ui && npx tsc --noEmit`
Expected: no new errors

- [ ] **Step 3: Commit**

```bash
git add ui/src/lib/api.ts
git commit -m "feat(ui): client calls for panel preview, rescope, and default range"
```

---

### Task 11: `Panel.svelte` range control UI

**Files:**
- Modify: `ui/src/Panel.svelte`
- Test: `ui/e2e/time-selector.spec.ts` (new; mirror the structure of `ui/e2e/yview.spec.ts`)

**Interfaces:**
- Consumes: `isInBounds`, `presetRange`, `parseRelative` (Task 9); `previewPanel`,
  `rescopePanel` (Task 10); the existing `fetchPanelData`, `pending`/`pick` state pattern
  already in `Panel.svelte` for y-view (lines 153-215) as the template for how a
  server-round-trip control manages optimistic/pending/error state in this file.

- [ ] **Step 1: Read the existing y-view control block for the exact state-management idiom**

Read `ui/src/Panel.svelte` lines 1-50 (imports) and 145-220 (the `pending`/`pick`/`error`
state block for y-views) in full before writing this task's code — reuse the same
`$state`/`.catch()`/`.finally()` idiom, not a new one.

- [ ] **Step 2: Add viewport state and the preset/typed/absolute controls**

Add near the other `$state` declarations (around line 153):

```typescript
  import { isInBounds, parseRelative, presetRange, type Viewport } from "./chart/viewport";
  import { previewPanel, rescopePanel } from "./lib/api";

  let viewport = $state<Viewport | null>(null); // null = showing the panel's committed range
  let previewData = $state<{ dataset: string; summary: unknown } | null>(null);
  let rangeBusy = $state(false);
  let rangeError = $state<string | null>(null);

  const fetchedBounds = () => ({
    start_ms: panel.spec.scope?.time_range?.start_ms ?? 0,
    end_ms: panel.spec.scope?.time_range?.end_ms ?? 0,
  });

  const applyViewport = (v: Viewport) => {
    viewport = v;
    rangeError = null;
    if (isInBounds(v, fetchedBounds())) {
      previewData = null; // in-bounds: client-side resample only, no preview needed
      return;
    }
    rangeBusy = true;
    previewPanel(panel.id, String(v.start_ms), String(v.end_ms))
      .then((r) => (previewData = r))
      .catch((e) => (rangeError = String(e)))
      .finally(() => (rangeBusy = false));
  };

  const pickPreset = (preset: "15m" | "1h" | "6h" | "24h" | "7d") =>
    applyViewport(presetRange(preset, Date.now()));

  const pickTyped = (text: string) => {
    const v = parseRelative(text, Date.now());
    if (v) applyViewport(v);
    else rangeError = `could not parse ${text!r}`;
  };

  const keepThisRange = () => {
    if (!viewport) return;
    rangeBusy = true;
    rescopePanel(panel.id, String(viewport.start_ms), String(viewport.end_ms))
      .then((r) => {
        viewport = null;
        previewData = null;
        // the caller (whatever renders the panel list) is responsible for swapping
        // the visible panel id to r.panel.id — out of scope for this component.
      })
      .catch((e) => (rangeError = String(e)))
      .finally(() => (rangeBusy = false));
  };
```

Note: `rangeError = \`could not parse ${text!r}\`` is invalid TypeScript (that's Python
syntax) — write it as `` `could not parse ${text}` `` instead.

- [ ] **Step 3: Add the control markup**

Add a new `<div class="legend">` block near the other legend rows (e.g. right after the
y-views block, around line 801):

```svelte
  {#if data?.kind === "time"}
    <div class="legend time-range" role="group" aria-label="Time range">
      range:
      {#each ["15m", "1h", "6h", "24h", "7d"] as const as preset}
        <button type="button" disabled={rangeBusy} onclick={() => pickPreset(preset)}>{preset}</button>
      {/each}
      <input
        type="text" placeholder="now-3h" disabled={rangeBusy}
        onkeydown={(e) => e.key === "Enter" && pickTyped((e.target as HTMLInputElement).value)}
      />
      {#if previewData}
        <span class="hint" data-preview-badge>previewing a different range</span>
        <button type="button" disabled={rangeBusy} onclick={keepThisRange}>keep this range</button>
      {/if}
      {#if rangeError}<span class="hint" data-range-error>{rangeError}</span>{/if}
    </div>
  {/if}
```

- [ ] **Step 4: Gate the selection menu's ask/annotate actions while previewing**

Find where `<SelectionMenu ...>` is rendered (around line 734) and add a guard: only render
it when not previewing an uncommitted range:

```svelte
    {#if selection && !previewData}
```

(replacing the existing `{#if selection}` on that line). Add a hint next to the range
controls when both are true:

```svelte
      {#if previewData}
        <span class="hint">keep this range to annotate or ask about it</span>
      {/if}
```

(this can be folded into the existing `{#if previewData}` block from Step 3 rather than
duplicated — add this line inside that block)

- [ ] **Step 5: Manual check in the running app**

Run: `cd ui && npm run dev` (or whatever the project's existing dev-server command is — check
`ui/package.json` `scripts` first)

Open a time-series panel, click a preset outside its current range, confirm the "previewing"
badge appears and the selection menu no longer opens on drag; click "keep this range" and
confirm a new panel appears.

- [ ] **Step 6: Write the e2e test**

Read `ui/e2e/yview.spec.ts` in full first and mirror its fixture/setup exactly. Then add:

```typescript
// ui/e2e/time-selector.spec.ts
import { expect, test } from "@playwright/test";
// mirror the setup helpers yview.spec.ts uses (imports, page navigation, panel creation) —
// copy that file's top-of-file setup verbatim before this test.

test("preset range change previews, then keep-this-range creates a new panel", async ({ page }) => {
  // ... reuse yview.spec.ts's panel-creation setup to get to a page with one time-series panel ...
  await page.getByRole("button", { name: "6h" }).click();
  await expect(page.locator("[data-preview-badge]")).toBeVisible();
  const panelCountBefore = await page.locator(".panel").count();
  await page.getByRole("button", { name: "keep this range" }).click();
  await expect(page.locator(".panel")).toHaveCount(panelCountBefore + 1);
});
```

- [ ] **Step 7: Run the e2e test**

Run: `cd ui && npx playwright test time-selector.spec.ts`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add ui/src/Panel.svelte ui/e2e/time-selector.spec.ts
git commit -m "feat(ui): time-range selector on panels (presets, preview, keep-this-range)"
```

---

## Self-Review Notes

- **Spec coverage:** Tiers 1/2/3 → Tasks 9-11 (viewport, preview, rescope wiring). `rescope()`
  backend → Tasks 5-7. `preview()` → Task 4. Mark scope cut (line+envelope/fleet/spc only,
  `rescope_unsupported_for_mark` for the rest) → Tasks 5-7, tested explicitly. Workspace
  default range → Tasks 1-2. `panel.rescoped` event → Task 3. HTTP routes → Task 8. Frontend
  client → Task 10. Deferred items (brush-zoom, seasonal/littles/spectrogram rescope,
  auto-crystallize-on-annotate) are explicitly not tasks here, per the spec's "Deferred"
  section.
- **Placeholder scan:** none found on final pass; Task 11 Step 2 flags its own
  Python-syntax typo inline so the executor doesn't copy it verbatim.
- **Type consistency:** `preview()`/`rescope()` signatures (`panel_id, start, end, actor`)
  match across Tasks 4-8 and the frontend calls in Task 10. `Viewport` shape
  (`start_ms`/`end_ms`) matches between Task 9's implementation and Task 11's usage.
- **Known risk to flag to a reviewer:** Task 8's test fixture names (`client`,
  `svc_with_panel`) and Task 11's dev-server command are placeholders for "whatever this repo
  already uses" — Step 1 of each task requires reading the existing file first to get the real
  names before writing code. This is intentional (the plan was written without running the
  existing test suite's fixtures), but an executor must not skip that reconnaissance step.
