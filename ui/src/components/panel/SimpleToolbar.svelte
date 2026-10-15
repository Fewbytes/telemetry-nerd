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
  // Claude's y-view suggestions (the nerd row's "· Claude:" buttons), as option values "s:<id>"
  const suggestions = $derived(offers.length > 0 ? panel.spec.y.views ?? [] : []);
  const activeKey = $derived(offers.find((o) => yViewIsOn(o, chosen, bandPick)));
  const yValue = $derived(
    chosen?.id && suggestions.some((v) => v.id === chosen.id) ? `s:${chosen.id}` : activeKey ? yViewKey(activeKey) : "",
  );
  const chips = $derived(data?.kind === "time" && data.overlays?.flags ? overlayChips(data.overlays) : []);
  const onChips = $derived(chips.filter((c) => c.on).length);

  const applyView = (key: string) => {
    const v = key.startsWith("s:") ? suggestions.find((s) => s.id === key.slice(2)) : undefined;
    if (v) return pick({ suggestion: v.id! }, v);
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
      <select aria-label="Y-axis view" value={yValue} disabled={idxBusy}
        onchange={(e) => {
          applyView(e.currentTarget.value);
          e.currentTarget.value = yValue; // the panel's state decides what is shown (a failed pick shows the old view)
        }}>
        {#each offers as o (yViewKey(o))}
          <option value={yViewKey(o)} disabled={!o.enabled} title={o.title}>{o.label}</option>
        {/each}
        {#if suggestions.length}
          <optgroup label="Claude suggests">
            {#each suggestions as v (v.id)}
              <option value={`s:${v.id}`} title={v.reason ?? ""} data-y-suggestion={v.id}>{v.label}</option>
            {/each}
          </optgroup>
        {/if}
      </select>
      {#if bandPick}<span class="hint">drag vertically on the plot · Esc cancels</span>{/if}
      {#if idxBusy}<span class="hint">fetching baseline…</span>{/if}
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
