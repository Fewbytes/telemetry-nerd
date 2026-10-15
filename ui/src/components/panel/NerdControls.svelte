<script lang="ts">
  // the control rows under the plot (nerd mode). Markup only: the state and handlers live in
  // Panel, which passes them in, so other controls (e.g. a compact toolbar) can share them.
  import type { Panel, PanelData, YContext, YView, selectYView } from "../../lib/api";
  import type { Viewport } from "../../chart/viewport";
  import type { PreviewRender as PreviewRenderData } from "../../chart/preview";
  import { VIEW_LABELS, type DataViewName } from "../../chart/dataview";
  import { overlayChips } from "../../chart/overlays";
  import { yViewAction, yViewIsOn, yViewKey, yViewOffers, type YStats } from "../../chart/yview";
  import { intervalLegend } from "../../lib/panelNotes";
  import RangeStatus from "./RangeStatus.svelte";
  import PreviewRender from "./PreviewRender.svelte";

  let {
    data, panel, yst, yctx, yIsValues, chosen, bandPick = $bindable(), idxBusy, indexedOn,
    overlayBusy, splitBusy, splitNote, reframeBusy, margBusy, rangeBusy, rangeError,
    viewport, previewRender, viewNow, previewEl = $bindable(),
    toggleOverlay, split, reframe, pick, pickIndexed, pickView, pickPreset, pickTyped, keepThisRange, toggleMarginal,
  }: {
    data: PanelData | null;
    panel: Panel;
    yst: YStats | null;
    yctx: YContext | null;
    yIsValues: boolean;
    chosen: YView | null;
    bandPick: boolean;
    idxBusy: boolean;
    indexedOn: boolean;
    overlayBusy: boolean;
    splitBusy: boolean;
    splitNote: string | null;
    reframeBusy: boolean;
    margBusy: boolean;
    rangeBusy: boolean;
    rangeError: string | null;
    viewport: Viewport | null;
    previewRender: PreviewRenderData | null;
    viewNow: DataViewName | null;
    previewEl: HTMLDivElement | null;
    toggleOverlay: (key: "normal" | "limit" | "ghost", on: boolean) => void;
    split: () => void;
    reframe: (i: number) => void;
    pick: (body: Parameters<typeof selectYView>[1], optimistic: YView | null) => void;
    pickIndexed: (baseline: "window" | "previous" | "week") => void;
    pickView: (v: DataViewName) => void;
    pickPreset: (preset: "15m" | "1h" | "6h" | "24h" | "7d") => void;
    pickTyped: (text: string) => void;
    keepThisRange: () => void;
    toggleMarginal: (ref: "previous" | "week" | "profile" | null) => void;
  } = $props();
</script>

{#if data?.kind === "time" && data.overlays?.flags}
  <div class="legend overlays" role="group" aria-label="Reference layers">
    layers:
    {#each overlayChips(data.overlays) as c (c.key)}
      <button
        type="button" data-overlay={c.key} title={c.title} class:on={c.on}
        disabled={!c.enabled || overlayBusy} aria-pressed={c.on}
        onclick={() => toggleOverlay(c.key, !c.on)}
      >{c.label}</button>
    {/each}
  </div>
{/if}
{#if data?.dataset.histogram}
  <div class="legend reframes" role="group" aria-label="Follow-ups">
    follow-up:
    <button type="button" data-follow-up="split-outcome" disabled={splitBusy}
      title="Fast errors flatter latency and slow errors hide in it: show successful and failed requests apart (two new panels; this one stays)."
      onclick={split}>split by success / failure</button>
    {#if splitNote}<span class="hint" data-split-note>{splitNote}</span>{/if}
  </div>
{/if}
{#if data?.kind === "time" && yctx?.reframes?.length}
  <div class="legend reframes" role="group" aria-label="Reframings">
    reframe:
    {#each yctx.reframes as r, i (r.title)}
      <button
        type="button" data-reframe={i} title={`${r.reason} (${r.basis}). Opens a new panel; this one stays.`}
        disabled={reframeBusy} onclick={() => reframe(i)}
      >{r.title}</button>
    {/each}
  </div>
{/if}
{#if (data?.kind === "time" || data?.kind === "fleet") && yIsValues && yst}
  <div class="legend y-views" role="group" aria-label="Y-axis view">
    y:
    {#each yViewOffers(data?.kind ?? "", yst, yctx) as o (yViewKey(o))}
      <button
        type="button" disabled={!o.enabled} title={o.title} class:suggest={o.suggest}
        class:on={yViewIsOn(o, chosen, bandPick)}
        onclick={() => {
          const a = yViewAction(o);
          if (a.kind === "band") bandPick = !bandPick;
          else if (a.kind === "indexed") pickIndexed(a.baseline);
          else pick(a.body, a.optimistic);
        }}
      >{o.label}</button>
    {/each}
    {#if (panel.spec.y.views ?? []).length}
      · Claude:
      {#each panel.spec.y.views ?? [] as v (v.id)}
        <button type="button" class="suggested" class:on={chosen?.id === v.id} title={v.reason ?? ""} data-y-suggestion={v.id} onclick={() => pick({ suggestion: v.id! }, v)}>{v.label}</button>
      {/each}
    {/if}
    {#if bandPick}<span class="hint">drag vertically on the plot · Esc cancels</span>{/if}
    {#if idxBusy}<span class="hint">fetching baseline…</span>{/if}
  </div>
{/if}
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
    <RangeStatus {viewport} {previewRender} {rangeBusy} {rangeError} {keepThisRange} />
  </div>
  <PreviewRender {panel} {previewRender} bind:previewEl />
{/if}
{#if data?.kind === "time" && data.filter}
  <div class="legend y-views" role="group" aria-label="Data view">
    data:
    {#each data.filter.offered as v (v)}
      <button type="button" class:on={viewNow === v} data-view={v}
        title={v === data.filter.default ? `Claude's pick: ${data.filter.reason}` : ""}
        onclick={() => pickView(v)}>{VIEW_LABELS[v]}{v === data.filter.default ? " · Claude" : ""}</button>
    {/each}
    <span class="hint">{data.filter.filter}; dashed: filter edge (unreliable)</span>
  </div>
{/if}
{#if data?.kind === "time" && data.series.some((s) => s.lo)}
  {@const bandLegend = intervalLegend(data.dataset)}
  {#if bandLegend}<div class="legend" data-interval-legend>band: {bandLegend}</div>{/if}
{/if}
{#if data?.kind === "time"}
  {@const fixed = data.dataset.producer?.kind === "code"}
  <div class="legend y-views" role="group" aria-label="Marginal histogram">
    marginal:
    <button type="button" class:on={!panel.spec.marginal} onclick={() => toggleMarginal(null)}>off</button>
    {#each [["previous", "vs previous time range"], ["week", "vs last week"], ["profile", "vs normal profile"]] as [r, label] (r)}
      <button type="button" disabled={indexedOn || margBusy || fixed}
        title={fixed ? "a code output is fixed data: no reference time range to fetch" : indexedOn ? "the marginal shows values; it is off in the indexed view" : ""}
        class:on={panel.spec.marginal?.reference === r} data-marginal-ref={r}
        onclick={() => toggleMarginal(r as "previous" | "week" | "profile")}>{label}</button>
    {/each}
    {#if panel.spec.marginal?.author === "claude"}<span class="hint">Claude: {panel.spec.marginal.reason}</span>{/if}
    {#if margBusy}<span class="hint">fetching reference…</span>{/if}
  </div>
{/if}
