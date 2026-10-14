# Simple/Nerd Modes — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the mode store, header toggle, settings menu (beads `2knd.1`, `2knd.10`) and the no-behavior-change refactor that extracts nerd-only blocks from `Panel.svelte` (`2knd.2`). These unblock all simple-mode rendering work (`.3`–`.9`).

**Architecture:** `ModeStore` (Svelte 5 runes class, same shape as `ThemeStore`) holds a persisted *default* (`localStorage`, `simple` unless changed) and a per-session *override* (`sessionStorage`, set by the header toggle); effective = override ?? default, mirrored to `<html data-mode>`. `Panel.svelte`'s control rows and shown/notes blocks move into child components so later beads add `{#if}`s at component boundaries.

**Tech Stack:** Svelte 5 (runes), TypeScript, vitest (node env), Playwright e2e on the fixture PromQL source.

**Spec:** `docs/superpowers/specs/2026-10-14-simple-nerd-modes-design.md`

## Global Constraints

- Default mode is **simple**; storage keys are exactly `tn-mode-default` (localStorage) and `tn-mode` (sessionStorage); `<html data-mode="simple|nerd">`.
- Nerd mode must render **identically to today**. Existing UI + e2e suites must pass unchanged in assertions; the only harness change is pinning nerd as the default for tests (Task 1).
- Worktree + branch per bead; ff-merge to master after gates (`just lint && uv run pytest tests/unit -q`, `cd ui && npx vitest run && cd .. && just ui-check`, `just e2e`). Never edit the master checkout.
- Screenshots (later beads) light mode only. Vocabulary per `docs/glossary.md`.
- Use `fd`/`rg`, never `find`/`grep`. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

---

### Task 1: ModeStore + header toggle + test-harness pin (bead `2knd.1`)

**Files:**
- Create: `ui/src/lib/mode.svelte.ts`, `ui/src/lib/mode.test.ts`
- Modify: `ui/src/App.svelte` (header-controls, ~line 138), `ui/e2e/fixtures.ts` (add `page` override)
- Test: `ui/src/lib/mode.test.ts`, then full `just e2e`

**Interfaces:**
- Produces:
  ```ts
  export type Mode = "simple" | "nerd";
  export class ModeStore {
    default: Mode;                 // $state, persisted; initial "simple"
    override: Mode | null;         // $state, session-only
    effective: Mode;               // $derived: override ?? default
    setOverride(m: Mode): void;    // header toggle
    setDefault(m: Mode): void;     // settings; also clears override so the change is visible
  }
  export const mode: ModeStore;
  ```

- [ ] **Step 1: Write the failing tests** — `ui/src/lib/mode.test.ts`

```ts
import { flushSync } from "svelte";
import { afterEach, describe, expect, it } from "vitest";
import { ModeStore } from "./mode.svelte";

// node env: stub the DOM surfaces the store touches (same approach as theme.test.ts).
interface Fake {
  local: Map<string, string>;
  session: Map<string, string>;
  doc: { dataset: Record<string, string | undefined> };
}
let fake: Fake | null = null;

function storage(m: Map<string, string>) {
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => m.set(k, v) };
}
function install(opts: { local?: Record<string, string>; session?: Record<string, string> } = {}) {
  const local = new Map(Object.entries(opts.local ?? {}));
  const session = new Map(Object.entries(opts.session ?? {}));
  const doc = { dataset: {} as Record<string, string | undefined> };
  globalThis.window = { localStorage: storage(local), sessionStorage: storage(session) } as unknown as Window & typeof globalThis;
  globalThis.document = { documentElement: doc } as unknown as Document;
  fake = { local, session, doc };
  return fake;
}
afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  fake = null;
});

describe("ModeStore", () => {
  it("defaults to simple and applies data-mode", () => {
    install();
    const m = new ModeStore();
    flushSync();
    expect(m.effective).toBe("simple");
    expect(fake?.doc.dataset.mode).toBe("simple");
  });

  it("uses the saved default", () => {
    install({ local: { "tn-mode-default": "nerd" } });
    const m = new ModeStore();
    expect(m.effective).toBe("nerd");
    expect(fake?.doc.dataset.mode).toBe("nerd");
  });

  it("session override beats the default", () => {
    install({ local: { "tn-mode-default": "nerd" }, session: { "tn-mode": "simple" } });
    const m = new ModeStore();
    expect(m.effective).toBe("simple");
  });

  it("ignores corrupted stored values", () => {
    install({ local: { "tn-mode-default": "hotdog" }, session: { "tn-mode": "x" } });
    const m = new ModeStore();
    expect(m.default).toBe("simple");
    expect(m.override).toBeNull();
  });

  it("setOverride writes sessionStorage only and applies immediately", () => {
    install();
    const m = new ModeStore();
    m.setOverride("nerd");
    flushSync();
    expect(m.effective).toBe("nerd");
    expect(fake?.session.get("tn-mode")).toBe("nerd");
    expect(fake?.local.has("tn-mode-default")).toBe(false);
    expect(fake?.doc.dataset.mode).toBe("nerd");
  });

  it("setDefault persists, clears the override, and applies", () => {
    install({ session: { "tn-mode": "nerd" } });
    const m = new ModeStore();
    m.setDefault("simple");
    flushSync();
    expect(fake?.local.get("tn-mode-default")).toBe("simple");
    expect(m.override).toBeNull();
    expect(m.effective).toBe("simple");
    expect(fake?.doc.dataset.mode).toBe("simple");
  });

  it("keeps working when storage rejects writes", () => {
    const f = install();
    f.local.set = () => { throw new Error("quota"); };
    f.session.set = () => { throw new Error("quota"); };
    const m = new ModeStore();
    m.setOverride("nerd");
    m.setDefault("nerd");
    flushSync();
    expect(m.effective).toBe("nerd");
  });

  it("is importable without a window (node)", () => {
    const m = new ModeStore();
    expect(m.effective).toBe("simple");
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run src/lib/mode.test.ts`
Expected: FAIL — cannot resolve `./mode.svelte`.

- [ ] **Step 3: Implement** — `ui/src/lib/mode.svelte.ts`

```ts
// Mode store: "simple" (minimal panels) or "nerd" (everything). Two layers:
//  - default: a user setting, persisted in localStorage, "simple" unless changed;
//  - override: the header toggle, per browser session (sessionStorage).
// effective = override ?? default, mirrored to data-mode on <html>.
//
// DOM guards keep the module importable in node-environment vitest tests; tests stub
// window/document (see mode.test.ts), like theme.svelte.ts.

export type Mode = "simple" | "nerd";

const DEFAULT_KEY = "tn-mode-default";
const SESSION_KEY = "tn-mode";

function read(area: "localStorage" | "sessionStorage", key: string): Mode | null {
  try {
    const v = window[area].getItem(key);
    return v === "simple" || v === "nerd" ? v : null;
  } catch {
    return null;
  }
}

function write(area: "localStorage" | "sessionStorage", key: string, v: Mode): void {
  try {
    window[area].setItem(key, v);
  } catch {
    /* private mode etc. — the choice still applies for this page load */
  }
}

export class ModeStore {
  default = $state<Mode>(typeof window === "undefined" ? "simple" : (read("localStorage", DEFAULT_KEY) ?? "simple"));
  override = $state<Mode | null>(typeof window === "undefined" ? null : read("sessionStorage", SESSION_KEY));

  effective: Mode = $derived(this.override ?? this.default);

  constructor() {
    if (typeof document !== "undefined") this.apply();
  }

  setOverride(m: Mode): void {
    this.override = m;
    write("sessionStorage", SESSION_KEY, m);
    this.apply();
  }

  // Changing the default clears the override: otherwise the setting would appear to do nothing.
  setDefault(m: Mode): void {
    this.default = m;
    this.override = null;
    write("localStorage", DEFAULT_KEY, m);
    try {
      window.sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* as above */
    }
    this.apply();
  }

  // Imperative (not an $effect): deterministic and testable outside a component.
  private apply(): void {
    document.documentElement.dataset.mode = this.effective;
  }
}

export const mode = new ModeStore();
```

Note: the test stub's storage has no `removeItem`; the `try/catch` covers it, but add `removeItem: (k: string) => m.delete(k)` to the test's `storage()` helper so the "clears the override" test also asserts `fake.session.has("tn-mode") === false`.

- [ ] **Step 4: Run to verify pass**

Run: `cd ui && npx vitest run src/lib/mode.test.ts`
Expected: PASS (8 tests).

- [ ] **Step 5: Header toggle** — `ui/src/App.svelte`: add `import { mode } from "./lib/mode.svelte";` next to the `theme` import, and put this as the first child of `<div class="header-controls">`:

```svelte
      <span class="mode-toggle" role="group" aria-label="Display mode">
        {#each ["simple", "nerd"] as const as m (m)}
          <button
            type="button" data-mode-toggle={m} aria-pressed={mode.effective === m}
            class:on={mode.effective === m} onclick={() => mode.setOverride(m)}
          >{m}</button>
        {/each}
      </span>
```
Add CSS in the same `<style>` block, matching the existing `.theme-toggle` sizing: `.mode-toggle { display: inline-flex; } .mode-toggle button { padding: 2px 10px; } .mode-toggle button.on { font-weight: 600; background: var(--accent-bg, var(--grid)); }` (reuse an existing token if `--accent-bg` is absent).

- [ ] **Step 6: Pin nerd for the existing e2e suite** — `ui/e2e/fixtures.ts`: inside `base.extend<Isolation>({`, add after `context:`

```ts
  // The default mode is "simple" (bead 2knd). Existing specs assert nerd rendering, so they run in
  // nerd unless a spec opts out (e.g. test.use / page.evaluate to set tn-mode-default).
  page: async ({ page }, use) => {
    await page.addInitScript(() => {
      if (!localStorage.getItem("tn-mode-default")) localStorage.setItem("tn-mode-default", "nerd");
    });
    await use(page);
  },
```

- [ ] **Step 7: Gates** — `cd ui && npx vitest run && cd .. && just ui-check && just e2e`. Expected: all pass, no assertion edits in any existing spec.

- [ ] **Step 8: Commit**

```bash
git add ui/src/lib/mode.svelte.ts ui/src/lib/mode.test.ts ui/src/App.svelte ui/e2e/fixtures.ts
git commit -m "feat(ui): mode store, header toggle, nerd pinned for e2e (telemetry-nerd-2knd.1)"
```

---

### Task 2: SettingsMenu with default-mode select (bead `2knd.10`)

**Files:**
- Create: `ui/src/components/SettingsMenu.svelte`, `ui/e2e/settings-mode.spec.ts`
- Modify: `ui/src/App.svelte` (mount the menu after `ConnectionPill`)

**Interfaces:**
- Consumes: `mode` from Task 1 (`mode.default`, `mode.setDefault`).
- Produces: `<SettingsMenu />` (no props).

- [ ] **Step 1: Write the failing e2e** — `ui/e2e/settings-mode.spec.ts`

```ts
import { test, expect } from "./fixtures.js";

test("default mode is changed in settings and survives a reload", async ({ page }) => {
  // fixtures pin nerd; clear it to observe the real default
  await page.addInitScript(() => localStorage.removeItem("tn-mode-default"));
  await page.goto("/");
  await page.evaluate(() => localStorage.removeItem("tn-mode-default"));
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");

  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Default mode").selectOption("nerd");
  await expect(page.locator("html")).toHaveAttribute("data-mode", "nerd");

  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "nerd");
});

test("header toggle overrides for the session only", async ({ page }) => {
  await page.goto("/");
  await page.locator("[data-mode-toggle=simple]").click();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");
  expect(await page.evaluate(() => localStorage.getItem("tn-mode-default"))).toBe("nerd");
});
```
Caution: the `addInitScript` that removes the key runs before the fixture's pin on every navigation, which would defeat reload persistence; if the first test flakes on that, replace it with a plain `page.evaluate` removal after `goto` and drop the `addInitScript` line (the fixture's init script only sets the key when absent, so the removal-then-reload sequence must be done once, then the select persists it).

- [ ] **Step 2: Run to verify failure**

Run: `just e2e -- settings-mode` (or `cd ui && npx playwright test settings-mode`)
Expected: FAIL — no "Settings" button.

- [ ] **Step 3: Implement** — `ui/src/components/SettingsMenu.svelte`

```svelte
<script lang="ts">
  import { mode, type Mode } from "../lib/mode.svelte";

  let open = $state(false);
  let root = $state<HTMLElement>();

  $effect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root && !root.contains(e.target as Node)) open = false;
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && (open = false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  });
</script>

<span class="settings" bind:this={root}>
  <button type="button" aria-haspopup="true" aria-expanded={open} onclick={() => (open = !open)}>Settings</button>
  {#if open}
    <div class="pop" role="dialog" aria-label="Settings">
      <label>
        Default mode
        <select value={mode.default} onchange={(e) => mode.setDefault(e.currentTarget.value as Mode)}>
          <option value="simple">simple</option>
          <option value="nerd">nerd</option>
        </select>
      </label>
      <p class="hint">Simple: minimal panels. Nerd: everything. The header toggle overrides this for the current session.</p>
    </div>
  {/if}
</span>

<style>
  .settings { position: relative; display: inline-block; }
  .pop { position: absolute; right: 0; top: 100%; z-index: 20; min-width: 16rem; padding: 0.75rem; background: var(--bg); border: 1px solid var(--grid); border-radius: 6px; }
  .hint { color: var(--muted); font-size: 0.85em; margin: 0.5rem 0 0; }
</style>
```
In `App.svelte` import it and render `<SettingsMenu />` right after `<ConnectionPill .../>`.

- [ ] **Step 4: Run to verify pass**

Run: `cd ui && npx playwright test settings-mode`
Expected: PASS. Then full `just ui-check` (svelte-check a11y: the dialog needs no interactive-role warnings; fix any that appear).

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/SettingsMenu.svelte ui/src/App.svelte ui/e2e/settings-mode.spec.ts
git commit -m "feat(ui): settings menu with default mode (telemetry-nerd-2knd.10)"
```

---

### Task 3: Extract nerd-only blocks from Panel.svelte (bead `2knd.2`, **Opus**)

Pure refactor. **No behavior, markup, class, `data-*` attribute or text change** for nerd mode. Proof: all existing vitest + e2e pass with zero assertion edits, and a DOM snapshot comparison (Step 1) is identical before/after.

**Files:**
- Create: `ui/src/components/panel/NerdControls.svelte`, `ui/src/components/panel/ShownBlock.svelte`, `ui/src/components/panel/NotesList.svelte`
- Modify: `ui/src/Panel.svelte` (markup from `{#if data?.kind === "time" && data.bucket_state_more}` through the end of the `{#if data}` notes block; ~lines 1023–1160 at plan time — re-locate with `rg -n 'legend overlays|class="shown"|class="notes"' ui/src/Panel.svelte`)
- Move styles: any `.legend`, `.shown`, `.notes`, `.note`, `.hint`, `.preview-render` rules in Panel's `<style>` that only apply to moved markup go with it (Svelte scopes styles per component; verify by diffing the rendered computed styles in Step 1).

**Interfaces (produced; later beads consume):**
- `NerdControls.svelte` renders, in order, exactly the existing blocks: reference layers, follow-up, reframe, y-views, time-range (+ `preview-render`), data filter, band legend, marginal. Props are the state and handlers those blocks read today, passed through unchanged:
  `data, panel, yst, yctx, yIsValues, chosen, bandPick (bindable), idxBusy, indexedOn, overlayBusy, splitBusy, splitNote, reframeBusy, margBusy, rangeBusy, rangeError, viewport, previewRender, viewNow, toggleOverlay, split, reframe, pick, pickIndexed, pickView, pickPreset, pickTyped, keepThisRange, toggleMarginal, previewEl (bindable)`.
  Derive the exact list by reading the markup being moved; any identifier it references that is not a prop or import is a missing prop.
- `ShownBlock.svelte` props: `data, heatView, fleetView, openCode` (renders `.shown` and the `<details class="query">`).
- `NotesList.svelte` props: `notes, activeNotes, setFocus` (renders `ul.notes`).
- Each toolbar-ish concern keeps its handler **in Panel.svelte**; components receive callbacks. Do not move fetch/state logic.

- [ ] **Step 1: Characterization baseline.** On a clean worktree of master: run `cd ui && npx vitest run` and `just e2e`; record pass counts. Add a throwaway Playwright script (not committed) that opens a fixture workspace with a time panel, a heatmap panel and a fleet panel, and saves `page.locator(".panel").evaluateAll(els => els.map(e => e.outerHTML))` to `$SCRATCH/before.json`. Same for each kind that has control rows.
- [ ] **Step 2: Extract `NotesList` first** (smallest). Move the `<ul class="notes">…</ul>` markup and its CSS; pass props. Run vitest + the panel-notes e2e specs (`rg -l 'data-note' ui/e2e`).
- [ ] **Step 3: Extract `ShownBlock`**, then re-run the same checks.
- [ ] **Step 4: Extract `NerdControls`** one block at a time (layers → follow-up → reframe → y-views → time-range → filter → band legend → marginal), running `npx vitest run` and the related e2e spec (`rg -l 'data-overlay|data-reframe|data-y-suggestion|data-marginal-ref|data-view' ui/e2e`) after each block. Two-way binds (`bandPick`, `previewEl`) use `$bindable()`.
- [ ] **Step 5: Verify identity.** Regenerate the DOM dump as `after.json`; the diff must show only Svelte scoping-class hash changes (`svelte-xxxx`), nothing else. Run full gates: `cd ui && npx vitest run && cd .. && just ui-check && just e2e`. Expected: same pass counts as Step 1; `Panel.svelte` is >250 lines shorter.
- [ ] **Step 6: Commit** (one commit per extracted component is fine)

```bash
git add ui/src/Panel.svelte ui/src/components/panel
git commit -m "refactor(ui): extract NerdControls, ShownBlock, NotesList from Panel (telemetry-nerd-2knd.2)"
```

---

## Self-review

- **Spec coverage:** default simple + session override + settings (Tasks 1–2); `data-mode` (Task 1); refactor first (Task 3); harness pinning nerd (Task 1 Step 6). Simple-mode rendering, toolbar, peek, sidebar, kinds, e2e/screenshots/docs are beads `.3`–`.9`, planned after this lands (per-kind inventory in `.8` needs its own pass).
- **Placeholders:** Task 3 is a move, so it specifies the procedure and verification instead of repeating ~140 lines of markup; the prop list is derived mechanically from the moved markup.
- **Type consistency:** `Mode`, `mode.effective`, `mode.setOverride`, `mode.setDefault`, `mode.default`, storage keys are the same in Tasks 1–2 and the spec.
- **Ordering:** Task 1 and Task 3 are independent and can run in parallel worktrees; Task 2 depends on Task 1.
