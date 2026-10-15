# Simple/Nerd Modes — Simple Panels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make simple mode real for panels and the sidebar (beads `2knd.3` chrome, `2knd.4` toolbar, `2knd.7` sidebar/highlights). Nerd mode stays identical.

**Architecture:** `Panel.svelte` gets one `simple` derived (`mode.effective === "simple"`; bead `.6` later swaps it for a per-panel peek-aware value) and uses it to skip nerd-only blocks. Controls become a compact `SimpleToolbar` above the plot that shares action logic with `NerdControls` through pure helpers in `chart/yview.ts`. Sidebar and HighlightStrip read the mode directly.

**Tech Stack:** Svelte 5 runes, TypeScript, vitest (node), Playwright e2e on the fixture PromQL source.

**Spec:** `docs/superpowers/specs/2026-10-14-simple-nerd-modes-design.md` (foundation shipped: `ui/src/lib/mode.svelte.ts`, header toggle `[data-mode-toggle]`, `NerdControls`/`ShownBlock`/`NotesList` in `ui/src/components/panel/`).

## Global Constraints

- Simple mode "same truth, less chrome": it must never hide a caveat/uncertainty that changes the reading. Kept in simple: question, plot, rug (missing data), annotations, `y ≠ 0` badge, band legend text, notes/caveats list (full text until bead `.5`), the brush **preview badge / "keep this range" / preview-render** (a preview is "not this panel's evidence").
- Nerd mode renders **identically to today**: same DOM, classes, `data-*`, text. Existing UI + e2e assertions are not edited. (e2e fixtures pin `tn-mode-default=nerd`; simple-mode specs click `[data-mode-toggle=simple]`.)
- Handlers/state stay in `Panel.svelte`; components get typed props and callbacks.
- Whitespace-significant text nodes (two continuation lines in `NerdControls`/`ShownBlock`) must keep their indent when code is moved again.
- Worktree + branch per bead group; ff-merge to master after gates: `cd ui && npx vitest run`, `just ui-check`, `just e2e` (`just ui-build` first when running Playwright directly). Never edit the master checkout. fd/rg only. Commit trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (Opus: `Claude Opus 5.5`).
- Screenshots (later bead `.9`) light mode only.

---

### Task 1: Simple panel chrome (bead `2knd.3`)

**Files:**
- Create: `ui/src/components/panel/OverflowMenu.svelte`, `ui/src/components/panel/SimpleMeta.svelte`, `ui/e2e/simple-panel.spec.ts`
- Modify: `ui/src/Panel.svelte`
- Test: `ui/e2e/simple-panel.spec.ts`

**Interfaces:**
- Consumes: `mode` from `ui/src/lib/mode.svelte.ts` (`mode.effective: "simple" | "nerd"`).
- Produces: in `Panel.svelte`, `const simple = $derived(mode.effective === "simple");` — later tasks/beads use `simple`. `<OverflowMenu label items />`, `<SimpleMeta data />`.

Simple-mode panel contents (everything else hidden, not rendered): header = collapse, question, status/answered-by, loading tag, refresh, `⋯` overflow (PNG, PDF), close; plot (+ `y ≠ 0` badge, rug, `rug-more`, annotations, linked-brush); `NotesList`; threads (unchanged until `.5`); one-line `SimpleMeta`. Hidden: `obj-id`, `PinButton`, the export group, marginal canvas (`marg` = null), resize handle, `NerdControls`, `ShownBlock`, `MetricCard`.

- [ ] **Step 1: Write the failing e2e** — `ui/e2e/simple-panel.spec.ts`

```ts
import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers";

test("simple mode: minimal chrome, essentials kept, nerd restored by the toggle", async ({ page }) => {
  const panel = await seedPanel(page.request, "simple: is the chrome minimal?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  // nerd (fixture pin): the parts exist
  await expect(el.locator(".obj-id").first()).toBeVisible();
  await expect(el.locator(".shown")).toBeVisible();
  await expect(el.locator("[data-metric-card]")).toHaveCount(1);
  await expect(el.locator("[data-export=png]")).toBeVisible();

  await page.locator("[data-mode-toggle=simple]").click();
  await expect(page.locator("html")).toHaveAttribute("data-mode", "simple");

  // kept
  await expect(el.locator(".question")).toContainText(panel.question);
  await expect(el.locator("[data-refresh]")).toBeVisible();
  await expect(el.locator("canvas").first()).toBeVisible();
  await expect(el.locator("[data-simple-meta]")).toContainText("step");
  // hidden
  await expect(el.locator(".obj-id")).toHaveCount(0);
  await expect(el.locator(".shown")).toHaveCount(0);
  await expect(el.locator("details.query")).toHaveCount(0);
  await expect(el.locator("[data-metric-card]")).toHaveCount(0);
  await expect(el.locator(".resize-handle")).toHaveCount(0);
  await expect(el.locator(".export-group")).toHaveCount(0);
  await expect(el.locator("[data-export=png]")).toHaveCount(0); // inside the closed menu

  // exports live in the overflow menu
  await el.getByRole("button", { name: "Panel actions" }).click();
  await expect(el.locator("[data-export=png]")).toBeVisible();
  await expect(el.locator("[data-export=pdf]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(el.locator("[data-export=png]")).toHaveCount(0);

  await page.locator("[data-mode-toggle=nerd]").click();
  await expect(el.locator(".shown")).toBeVisible();
});
```

- [ ] **Step 2: Run, verify FAIL** — `just ui-build && cd ui && npx playwright test simple-panel` → fails (`[data-simple-meta]` missing / `.obj-id` still present).

- [ ] **Step 3: `SimpleMeta.svelte`**

```svelte
<script lang="ts">
  // simple mode's one-line replacement for the "shown" block: time range and step. Full
  // provenance, dataset and query are nerd-only.
  import type { PanelData } from "../../lib/api";
  import { fmtStep } from "../../lib/format";

  let { data }: { data: PanelData } = $props();
  const fmtTime = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");
</script>

<p class="simple-meta" data-simple-meta>
  {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step {fmtStep(data.effective_step_ms)}
</p>
```
Add CSS to `ui/src/index.css` next to the `.shown` rules: `.simple-meta { margin: 0.25rem 0; color: var(--muted); font-size: 0.85em; }`.

- [ ] **Step 4: `OverflowMenu.svelte`** (click-outside + Escape + focus return; this is the pattern `SettingsMenu` should follow later)

```svelte
<script lang="ts">
  import type { Snippet } from "svelte";

  let { label, children }: { label: string; children: Snippet } = $props();
  let open = $state(false);
  let root = $state<HTMLElement>();
  let trigger = $state<HTMLButtonElement>();

  $effect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root && !root.contains(e.target as Node)) open = false;
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      open = false;
      trigger?.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  });
</script>

<span class="overflow" bind:this={root}>
  <button
    type="button" bind:this={trigger} aria-haspopup="true" aria-expanded={open} aria-label={label}
    onclick={() => (open = !open)}
  >⋯</button>
  {#if open}
    <span class="overflow-pop" role="group" aria-label={label}>{@render children()}</span>
  {/if}
</span>
```
CSS in `index.css`: `.overflow { position: relative; display: inline-block; } .overflow-pop { position: absolute; right: 0; top: 100%; z-index: 20; display: flex; flex-direction: column; gap: 2px; padding: 4px; background: var(--bg); border: 1px solid var(--grid); border-radius: 6px; }`.

- [ ] **Step 5: `Panel.svelte`** (locate with `rg -n 'class="obj-id"|PinButton|export-group|MetricCard|resize-handle|<NerdControls|<ShownBlock|const marg =' ui/src/Panel.svelte`)
  1. Imports: `import { mode } from "./lib/mode.svelte";`, `OverflowMenu`, `SimpleMeta`. Near the other deriveds: `const simple = $derived(mode.effective === "simple");`.
  2. `const marg = $derived(data?.kind === "time" && !indexedOn ? (data.marginal ?? null) : null);` → add `!simple &&` to the condition so the marginal canvas and its width reservation vanish.
  3. Header: wrap `<a class="obj-id" …>` and `<PinButton …/>` in `{#if !simple}…{/if}`. Replace the `<span class="export-group">…</span>` with:
     ```svelte
     {#if simple}
       <OverflowMenu label="Panel actions">
         <button type="button" class="export" data-export="png" disabled={!!exportBusy || collapsed} onclick={() => doExport("png")}>{exportBusy === "png" ? "…" : "Export PNG"}</button>
         <button type="button" class="export" data-export="pdf" disabled={!!exportBusy || collapsed} onclick={() => doExport("pdf")}>{exportBusy === "pdf" ? "…" : "Export PDF"}</button>
       </OverflowMenu>
     {:else}
       <!-- the existing export-group span, untouched -->
     {/if}
     ```
  4. Resize handle: add `!simple &&` to its `{#if}` condition.
  5. Below the plot: `<NerdControls …/>` → `{#if !simple}<NerdControls …/>{/if}`. `ShownBlock` → `{#if simple}<SimpleMeta {data} />{:else}<ShownBlock …/>{/if}` (keep `NotesList` after it, unconditionally). `<MetricCard …/>` → add `!simple &&` to its `{#if data}`.
  6. Leave `y-origin` badge, `rug-more`, threads as they are.

- [ ] **Step 6: Verify.** `just ui-build && cd ui && npx playwright test simple-panel` → PASS. Then full gates: `cd ui && npx vitest run && cd .. && just ui-check && just e2e` — no existing spec edited. Nerd DOM is untouched because every change is behind `simple`.

- [ ] **Step 7: Commit**

```bash
git add ui/src ui/e2e/simple-panel.spec.ts
git commit -m "feat(ui): simple panel chrome (telemetry-nerd-2knd.3)"
```

---

### Task 2: Shared y-view helpers + SimpleToolbar (bead `2knd.4`)

**Files:**
- Modify: `ui/src/chart/yview.ts` (add helpers), `ui/src/chart/yview.test.ts` (add tests; create if absent — check with `fd yview ui/src/chart`), `ui/src/components/panel/NerdControls.svelte` (use helpers + split out two fragments), `ui/src/Panel.svelte`
- Create: `ui/src/components/panel/RangeStatus.svelte`, `ui/src/components/panel/PreviewRender.svelte`, `ui/src/components/panel/SimpleToolbar.svelte`, `ui/e2e/simple-toolbar.spec.ts`

**Interfaces:**
- Produces in `chart/yview.ts`:
  ```ts
  export type YViewAction =
    | { kind: "band" }
    | { kind: "indexed"; baseline: "window" | "previous" | "week" }
    | { kind: "pick"; body: { mode: YView["mode"] }; optimistic: { mode: YView["mode"]; label: string } | null };
  export function yViewAction(o: Offer): YViewAction;
  export function yViewIsOn(o: Offer, chosen: YView | null, bandPick: boolean): boolean;
  /** offers shown for a panel kind: fleet drops indexed/meaningful/log */
  export function yViewOffers(kind: string, st: YStats, ctx: YContext | null): Offer[];
  export const yViewKey = (o: Offer): string => o.mode + (o.baseline ?? "");
  ```
- `RangeStatus` props: `{ viewport, previewRender, rangeBusy, rangeError, keepThisRange }` — renders (no wrapper element) the preview badge span, "keep this range" button and range-error span exactly as in `NerdControls` today.
- `PreviewRender` props: `{ panel, previewRender, previewEl = $bindable() }` — renders the `div.preview-render` block exactly as today.
- `SimpleToolbar` props: `{ data, panel, yst, yctx, yIsValues, chosen, bandPick = $bindable(), idxBusy, overlayBusy, rangeBusy, rangeError, viewport, previewRender, viewNow, previewEl = $bindable(), toggleOverlay, pick, pickIndexed, pickView, pickPreset, keepThisRange }` (same types as `NerdControls`).

- [ ] **Step 1: Failing unit tests** — append to `ui/src/chart/yview.test.ts`

```ts
import { describe, expect, it } from "vitest";
import { yViewAction, yViewIsOn, yViewOffers, yViewKey, type Offer } from "./yview";

const offer = (o: Partial<Offer> & Pick<Offer, "mode">): Offer =>
  ({ label: o.mode, enabled: true, suggest: false, title: "", ...o });

describe("yViewAction", () => {
  it("band toggles the vertical pick", () => {
    expect(yViewAction(offer({ mode: "band" }))).toEqual({ kind: "band" });
  });
  it("indexed carries its baseline", () => {
    expect(yViewAction(offer({ mode: "indexed", baseline: "week" }))).toEqual({ kind: "indexed", baseline: "week" });
  });
  it("auto picks with no optimistic view; others carry mode+label", () => {
    expect(yViewAction(offer({ mode: "auto" }))).toEqual({ kind: "pick", body: { mode: "auto" }, optimistic: null });
    expect(yViewAction(offer({ mode: "zero", label: "from zero" }))).toEqual({
      kind: "pick", body: { mode: "zero" }, optimistic: { mode: "zero", label: "from zero" },
    });
  });
});

describe("yViewIsOn", () => {
  it("no chosen view means auto is on", () => {
    expect(yViewIsOn(offer({ mode: "auto" }), null, false)).toBe(true);
    expect(yViewIsOn(offer({ mode: "zero" }), null, false)).toBe(false);
  });
  it("a suggestion (chosen.id) turns every mode off", () => {
    expect(yViewIsOn(offer({ mode: "zero" }), { mode: "zero", id: "s1", label: "x" } as never, false)).toBe(false);
  });
  it("band is on while picking or when chosen without an id", () => {
    expect(yViewIsOn(offer({ mode: "band" }), null, true)).toBe(true);
    expect(yViewIsOn(offer({ mode: "band" }), { mode: "band" } as never, false)).toBe(true);
  });
  it("indexed matches on baseline", () => {
    const ch = { mode: "indexed", baseline: "week" } as never;
    expect(yViewIsOn(offer({ mode: "indexed", baseline: "week" }), ch, false)).toBe(true);
    expect(yViewIsOn(offer({ mode: "indexed", baseline: "previous" }), ch, false)).toBe(false);
  });
});

describe("yViewKey", () => {
  it("is unique per mode+baseline", () => {
    expect(yViewKey(offer({ mode: "indexed", baseline: "week" }))).toBe("indexedweek");
    expect(yViewKey(offer({ mode: "auto" }))).toBe("auto");
  });
});
```
(`yViewOffers` is covered through the e2e + the existing `offeredViews` tests; add one test if the file already builds a `YStats` fixture helper.)

- [ ] **Step 2: Run, verify FAIL** — `cd ui && npx vitest run src/chart/yview.test.ts` → missing exports.

- [ ] **Step 3: Implement helpers** — append to `ui/src/chart/yview.ts` (the logic is lifted verbatim from the `NerdControls` y-views row: `onclick` and the `class:on` expression):

```ts
export type YViewAction =
  | { kind: "band" }
  | { kind: "indexed"; baseline: "window" | "previous" | "week" }
  | { kind: "pick"; body: { mode: YView["mode"] }; optimistic: { mode: YView["mode"]; label: string } | null };

export function yViewAction(o: Offer): YViewAction {
  if (o.mode === "band") return { kind: "band" };
  if (o.mode === "indexed") return { kind: "indexed", baseline: o.baseline! };
  return { kind: "pick", body: { mode: o.mode }, optimistic: o.mode === "auto" ? null : { mode: o.mode, label: o.label } };
}

export function yViewIsOn(o: Offer, chosen: YView | null, bandPick: boolean): boolean {
  if (o.mode === "band") return bandPick || (chosen?.mode === "band" && !chosen?.id);
  return (chosen?.mode ?? "auto") === o.mode && !chosen?.id && (o.baseline ?? null) === (chosen?.baseline ?? null);
}

export const yViewKey = (o: Offer): string => o.mode + (o.baseline ?? "");

export function yViewOffers(kind: string, st: YStats, ctx: YContext | null): Offer[] {
  return offeredViews(st, ctx).filter((o) => kind === "time" || !["indexed", "meaningful", "log"].includes(o.mode));
}
```
If `YView` has no `baseline`/`id` typing that compiles, mirror what `NerdControls` already relies on (it reads `chosen?.baseline`, `chosen?.id`) — adjust the helper's types, not the semantics.

- [ ] **Step 4: Run, verify PASS** — `npx vitest run src/chart/yview.test.ts`.

- [ ] **Step 5: Refactor `NerdControls` onto the helpers + split fragments (nerd DOM identical).**
  - y-views row: `{#each yViewOffers(data?.kind ?? "", yst, yctx) as o (yViewKey(o))}`; `class:on={yViewIsOn(o, chosen, bandPick)}`; `onclick={() => { const a = yViewAction(o); if (a.kind === "band") bandPick = !bandPick; else if (a.kind === "indexed") pickIndexed(a.baseline); else pick(a.body, a.optimistic); }}`. Everything else on the button unchanged.
  - Move the `{#if viewport && !rangeBusy && !rangeError}…{/if}` badge/keep block and `{#if rangeError}…{/if}` span into `RangeStatus.svelte` (fragment, same text, same attributes `data-preview-badge`, `data-range-error`; keep the deeper-indented continuation line as is) and render `<RangeStatus {viewport} {previewRender} {rangeBusy} {rangeError} {keepThisRange} />` at the same position inside `div.legend.time-range`. Move `{#if previewRender}<div class="preview-render" …>` into `PreviewRender.svelte` and render it right after the legend div.
  - **Verify identity**: before editing, dump `outerHTML` of `.panel` for a seeded time panel in nerd mode with and without a brushed preview (`brushMiddleThird` in `e2e/helpers.ts`) to the scratchpad; after editing, dump again; the diff must be only Svelte scoping-class hashes. Existing e2e (`rg -l 'data-preview|brushMiddleThird' ui/e2e`) must pass untouched.

- [ ] **Step 6: Failing e2e** — `ui/e2e/simple-toolbar.spec.ts`

```ts
import { expect, test } from "./fixtures.js";
import { brushMiddleThird, seedPanel } from "./helpers";

test("simple mode: one toolbar above the plot replaces the control rows", async ({ page }) => {
  const panel = await seedPanel(page.request, "simple: where are the controls?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();
  await page.locator("[data-mode-toggle=simple]").click();

  const bar = el.locator("[data-simple-toolbar]");
  await expect(bar).toBeVisible();
  // toolbar precedes the plot
  const barY = (await bar.boundingBox())!.y;
  const plotY = (await el.locator(".plot").boundingBox())!.y;
  expect(barY).toBeLessThan(plotY);
  // nerd control rows are gone
  await expect(el.locator(".legend.time-range, .legend.y-views, .legend.reframes")).toHaveCount(0);

  // range preset reuses the nerd handler (panel refetches with a new range)
  await bar.getByRole("button", { name: "6h", exact: true }).click();
  await expect(bar.getByRole("button", { name: "6h", exact: true })).toBeEnabled();

  // y-view dropdown applies a view
  const y = bar.getByLabel("Y-axis view");
  await y.selectOption({ label: "from zero" });
  await expect(el.locator("[data-y-origin]")).toHaveCount(0); // from-zero view has no y≠0 badge
  await expect(y.locator("option:checked")).toHaveText("from zero");

  // a brush selection still shows the preview badge and "keep this range" in simple mode
  await brushMiddleThird(page, panel.id);
  await expect(el.locator("[data-preview-badge]")).toBeVisible();
  await expect(el.getByRole("button", { name: "keep this range" })).toBeVisible();
});
```
(Use `seedPanel` options to get a time panel with enough range variation for the y selection; if "from zero" is disabled on the fixture series, pick the first enabled non-active option instead and assert the checked option changed.)

- [ ] **Step 7: Run, verify FAIL** (`[data-simple-toolbar]` missing).

- [ ] **Step 8: `SimpleToolbar.svelte`**

```svelte
<script lang="ts">
  // simple mode's compact controls: range presets left, view/layer/filter dropdowns right.
  // Same state and handlers as NerdControls (they live in Panel); only the presentation differs.
  import type { Panel, PanelData, YContext, YView, selectYView } from "../../lib/api";
  import type { Viewport } from "../../chart/viewport";
  import type { PreviewRender as PreviewRenderData } from "../../chart/preview";
  import { VIEW_LABELS, type DataViewName } from "../../chart/dataview";
  import { overlayChips } from "../../chart/overlays";
  import { yViewAction, yViewIsOn, yViewKey, yViewOffers, type YStats } from "../../chart/yview";
  import RangeStatus from "./RangeStatus.svelte";
  import PreviewRender from "./PreviewRender.svelte";

  let {
    data, panel, yst, yctx, yIsValues, chosen, bandPick = $bindable(), idxBusy, overlayBusy, rangeBusy, rangeError,
    viewport, previewRender, viewNow, previewEl = $bindable(),
    toggleOverlay, pick, pickIndexed, pickView, pickPreset, keepThisRange,
  }: {
    data: PanelData | null;
    panel: Panel;
    yst: YStats | null;
    yctx: YContext | null;
    yIsValues: boolean;
    chosen: YView | null;
    bandPick: boolean;
    idxBusy: boolean;
    overlayBusy: boolean;
    rangeBusy: boolean;
    rangeError: string | null;
    viewport: Viewport | null;
    previewRender: PreviewRenderData | null;
    viewNow: DataViewName | null;
    previewEl: HTMLDivElement | null;
    toggleOverlay: (key: "normal" | "limit" | "ghost", on: boolean) => void;
    pick: (body: Parameters<typeof selectYView>[1], optimistic: YView | null) => void;
    pickIndexed: (baseline: "window" | "previous" | "week") => void;
    pickView: (v: DataViewName) => void;
    pickPreset: (preset: "15m" | "1h" | "6h" | "24h" | "7d") => void;
    keepThisRange: () => void;
  } = $props();

  const offers = $derived(data && yst && yIsValues && (data.kind === "time" || data.kind === "fleet") ? yViewOffers(data.kind, yst, yctx) : []);
  const activeKey = $derived(offers.find((o) => yViewIsOn(o, chosen, bandPick)));
  const chips = $derived(data?.kind === "time" && data.overlays?.flags ? overlayChips(data.overlays) : []);
  const onChips = $derived(chips.filter((c) => c.on).length);

  const applyView = (key: string) => {
    const o = offers.find((x) => yViewKey(x) === key);
    if (!o) return;
    const a = yViewAction(o);
    if (a.kind === "band") bandPick = !bandPick;
    else if (a.kind === "indexed") pickIndexed(a.baseline);
    else pick(a.body, a.optimistic);
  };
</script>

{#if data && (data.kind === "time" || offers.length > 0)}
  <div class="simple-toolbar" data-simple-toolbar role="toolbar" aria-label="Panel controls">
    {#if data.kind === "time"}
      <span class="presets" role="group" aria-label="Time range">
        {#each ["15m", "1h", "6h", "24h", "7d"] as const as preset}
          <button type="button" disabled={rangeBusy} onclick={() => pickPreset(preset)}>{preset}</button>
        {/each}
      </span>
      <RangeStatus {viewport} {previewRender} {rangeBusy} {rangeError} {keepThisRange} />
    {/if}
    <span class="spacer"></span>
    {#if offers.length > 0}
      <select aria-label="Y-axis view" value={activeKey ? yViewKey(activeKey) : ""} disabled={idxBusy}
        onchange={(e) => applyView(e.currentTarget.value)}>
        {#each offers as o (yViewKey(o))}
          <option value={yViewKey(o)} disabled={!o.enabled} title={o.title}>{o.label}</option>
        {/each}
      </select>
      {#if bandPick}<span class="hint">drag vertically on the plot · Esc cancels</span>{/if}
    {/if}
    {#if data.kind === "time" && data.filter}
      <select aria-label="Data view" value={viewNow ?? ""} onchange={(e) => pickView(e.currentTarget.value as DataViewName)}>
        {#each data.filter.offered as v (v)}
          <option value={v}>{VIEW_LABELS[v]}{v === data.filter.default ? " · Claude" : ""}</option>
        {/each}
      </select>
    {/if}
    {#if chips.length > 0}
      <details class="layers">
        <summary aria-label="Reference layers">Layers{onChips ? ` (${onChips})` : ""}</summary>
        <div class="layers-pop">
          {#each chips as c (c.key)}
            <label title={c.title}>
              <input type="checkbox" checked={c.on} disabled={!c.enabled || overlayBusy}
                onchange={(e) => toggleOverlay(c.key, e.currentTarget.checked)} />
              {c.label}
            </label>
          {/each}
        </div>
      </details>
    {/if}
  </div>
  {#if data.kind === "time"}<PreviewRender {panel} {previewRender} bind:previewEl />{/if}
{/if}
```
Notes for the implementer: (a) `Chip.key` must be the type `toggleOverlay` accepts — mirror how `NerdControls` calls it; (b) keep the explanatory `title`s as written; (c) the nerd `y:` row also lists Claude's y-suggestions (`panel.spec.y.views`); in simple mode add them to the same `<select>` as an `<optgroup label="Claude suggests">` whose option values are `s:<id>` and whose change calls `pick({ suggestion: v.id! }, v)` — extend `applyView` accordingly and add `data-y-suggestion` to each option; (d) CSS in `index.css`: `.simple-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 0 0 4px; } .simple-toolbar .spacer { flex: 1; } .layers { position: relative; } .layers-pop { position: absolute; right: 0; z-index: 20; padding: 6px; background: var(--bg); border: 1px solid var(--grid); border-radius: 6px; display: flex; flex-direction: column; gap: 4px; }`.

- [ ] **Step 9: Mount in Panel.** Immediately before `<div bind:this={plotEl} class="plot" …>` (`rg -n 'bind:this={plotEl}' ui/src/Panel.svelte`):

```svelte
  {#if simple}
    <SimpleToolbar
      {data} {panel} {yst} {yctx} {yIsValues} {chosen} bind:bandPick {idxBusy} {overlayBusy} {rangeBusy} {rangeError}
      {viewport} {previewRender} {viewNow} bind:previewEl
      {toggleOverlay} {pick} {pickIndexed} {pickView} {pickPreset} {keepThisRange}
    />
  {/if}
```
Import `SimpleToolbar`. `NerdControls` stays `{#if !simple}` from Task 1. Note `previewEl` is bound in exactly one place at a time (toolbar in simple, `NerdControls` in nerd) — check Panel's preview `$effect` still fires after toggling modes with a preview open.

- [ ] **Step 10: Verify.** `just ui-build && cd ui && npx playwright test simple-toolbar simple-panel` → PASS; then `npx vitest run`, `just ui-check`, `just e2e` (no edits to existing specs).

- [ ] **Step 11: Commit** (helpers+tests / NerdControls refactor / toolbar as separate commits is fine)

```bash
git add ui/src ui/e2e/simple-toolbar.spec.ts
git commit -m "feat(ui): simple-mode toolbar with shared y-view helpers (telemetry-nerd-2knd.4)"
```

---

### Task 3: Slim Sidebar + HighlightStrip (bead `2knd.7`) — independent of Tasks 1–2

**Files:**
- Modify: `ui/src/components/Sidebar.svelte`, `ui/src/components/HighlightStrip.svelte`
- Create: `ui/e2e/simple-sidebar.spec.ts`

**Interfaces:** Consumes `mode` only. Produces nothing for other tasks.

Rulings baked in (spec says "slim form"): simple sidebar keeps **Chat** and **Findings** (the conversation and the conclusions) expanded as today; **Hypotheses** and **Gaps** become one-line headers with a count, collapsed until clicked (local state, so the persisted nerd fold preferences in `collapsedSidebar` are untouched); **Code runs** and **Annotations on closed panels** are not rendered. `HighlightStrip` drops the author badge. If any of this feels wrong in use, it is a one-file change.

- [ ] **Step 1: Failing e2e** — `ui/e2e/simple-sidebar.spec.ts`

```ts
import { expect, test } from "./fixtures.js";

test("simple mode: sidebar keeps chat and findings, folds hypotheses and gaps, hides code runs", async ({ page }) => {
  await page.goto("/");
  const side = page.locator("aside.sidebar");
  // nerd (fixture pin): every section present
  await expect(side.getByRole("heading", { name: "Hypotheses" })).toBeVisible();
  await expect(side.getByRole("heading", { name: "Gaps" })).toBeVisible();

  await page.locator("[data-mode-toggle=simple]").click();
  await expect(side.getByRole("heading", { name: "Chat" })).toBeVisible();
  await expect(side.getByRole("heading", { name: "Findings" })).toBeVisible();
  // hypotheses/gaps: header with a count, body hidden until expanded
  const hyp = side.getByRole("button", { name: /Hypotheses/ });
  await expect(hyp).toHaveAttribute("aria-expanded", "false");
  await expect(side.getByText("No hypotheses yet.")).toHaveCount(0);
  await hyp.click();
  await expect(hyp).toHaveAttribute("aria-expanded", "true");
  await expect(side.getByText("No hypotheses yet.")).toBeVisible();
  await expect(side.getByRole("heading", { name: "Code runs" })).toHaveCount(0);

  // switching back restores the persisted nerd layout
  await page.locator("[data-mode-toggle=nerd]").click();
  await expect(side.getByRole("heading", { name: "Gaps" })).toBeVisible();
});
```

- [ ] **Step 2: Run, verify FAIL** — `just ui-build && cd ui && npx playwright test simple-sidebar`.

- [ ] **Step 3: `Sidebar.svelte`.** Add `import { mode } from "../lib/mode.svelte";`, `const simple = $derived(mode.effective === "simple");`, and `let opened = $state(new Set<string>());` with `const toggleOpened = (k: string) => { const n = new Set(opened); n.has(k) ? n.delete(k) : n.add(k); opened = n; };`.
  - Define `const folded = (key: string) => simple ? !opened.has(key) && (key === "hypotheses" || key === "gaps") : collapsedSidebar.has(key);` and use it in place of `collapsedSidebar.has(...)` in the **Hypotheses** and **Gaps** sections' `aria-pressed/aria-expanded/title/aria-label/glyph/{#if}` expressions; their click handlers become `() => (simple ? toggleOpened(key) : collapsedSidebar.toggle(key))`. In simple mode append the count to the `<h2>`: `Hypotheses{#if simple} ({hypotheses.length}){/if}`, `Gaps{#if simple} ({gaps.length}){/if}`. Chat and Findings keep `collapsedSidebar` unchanged.
  - Wrap the **Annotations on closed panels** and **Code runs** sections in `{#if !simple}`; in `sections`/`allCollapsed`, `allCollapsed` is `false` when `simple`.
  - Nerd rendering must be byte-identical: `folded` falls through to `collapsedSidebar.has` when not simple.

- [ ] **Step 4: `HighlightStrip.svelte`.** `import { mode } from "../lib/mode.svelte";` and wrap the author badge: `{#if mode.effective === "nerd"}<span class="badge author {h.author}">{h.author}</span>{/if}`. (`data-highlight-author` stays on the `li`.)

- [ ] **Step 5: Verify.** `npx playwright test simple-sidebar` → PASS; full gates `cd ui && npx vitest run && cd .. && just ui-check && just e2e` with no edits to existing specs (e.g. `rg -l 'sidebar|highlight' ui/e2e` all run in the nerd pin).

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/Sidebar.svelte ui/src/components/HighlightStrip.svelte ui/e2e/simple-sidebar.spec.ts
git commit -m "feat(ui): slim sidebar and highlight strip in simple mode (telemetry-nerd-2knd.7)"
```

---

## Self-review

- **Spec coverage:** panel chrome table (Task 1: header, hidden shown/Query/MetricCard/resize/marginal, `range · step` line, `y ≠ 0`/rug/annotations kept, export overflow menu); compact toolbar with range presets, y-view, layers, series ("data") filter (Task 2); preview badge kept per "same truth" (Task 2); sidebar + highlight strip slim (Task 3). Not here, by design: compact notes + collapsed threads (`.5`, needs `1a8w`), peek (`.6`, swaps `simple` in Panel), group/non-time kinds (`.8`), docs/screenshots (`.9`). Non-time kinds fall through Task 1's generic hiding; their per-kind inventory is `.8`.
- **Types:** `simple` (Panel), `yViewAction/yViewIsOn/yViewKey/yViewOffers`, `RangeStatus`/`PreviewRender`/`SimpleToolbar` props and `[data-simple-toolbar]`, `[data-simple-meta]`, `Panel actions`, `Y-axis view`, `Data view` labels are used identically across tasks and tests.
- **Ordering:** Task 1 → Task 2 (toolbar replaces what Task 1 hides; between them simple mode has no controls). Task 3 is independent. Run Tasks 1–2 and 3 in separate worktrees if parallel, else sequentially on one branch.
- **Risk:** the `NerdControls` fragment split (Task 2 Step 5) touches whitespace-significant text; the DOM-identity dump is the gate. Dedupe of `chosen` types between helper and `YView` may need small type tweaks without changing semantics.
