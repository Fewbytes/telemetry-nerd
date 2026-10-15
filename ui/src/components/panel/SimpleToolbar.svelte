<script lang="ts">
  // simple mode's compact controls: range presets left, view/layer/filter dropdowns right.
  // Same state and handlers as NerdControls (they live in Panel); only the presentation differs.
  import { RANGE_PRESETS } from "../../chart/viewport";
  import { VIEW_LABELS, type DataViewName } from "../../chart/dataview";
  import { overlayChips } from "../../chart/overlays";
  import { yViewAction, yViewIsOn, yViewKey, yViewOffers } from "../../chart/yview";
  import RangeStatus from "./RangeStatus.svelte";
  import PreviewRender from "./PreviewRender.svelte";
  import type { ControlProps } from "./controls";

  let {
    data, panel, yst, yctx, yIsValues, chosen, bandPick = $bindable(), idxBusy, overlayBusy, rangeBusy, rangeError,
    viewport, previewRender, viewNow, previewEl = $bindable(),
    toggleOverlay, pick, pickIndexed, pickView, pickPreset, keepThisRange,
  }: ControlProps = $props();

  const offers = $derived(data && yst && yIsValues && (data.kind === "time" || data.kind === "fleet") ? yViewOffers(data.kind, yst, yctx) : []);
  // Claude's y-view suggestions (the nerd row's "· Claude:" buttons), as option values "s:<id>"
  const suggestions = $derived(offers.length > 0 ? panel.spec.y.views ?? [] : []);
  // a band pick in progress shows "band…" (yViewIsOn would match auto first while nothing is chosen)
  const activeOffer = $derived(
    bandPick ? offers.find((o) => o.mode === "band") : offers.find((o) => yViewIsOn(o, chosen, bandPick)),
  );
  const yValue = $derived(
    !bandPick && chosen?.id && suggestions.some((v) => v.id === chosen.id) ? `s:${chosen.id}`
      : activeOffer ? yViewKey(activeOffer) : "",
  );
  const chips = $derived(data?.kind === "time" && data.overlays?.flags ? overlayChips(data.overlays) : []);
  const onChips = $derived(chips.filter((c) => c.on).length);

  // a committed band is the select's value, and re-selecting the shown option fires no change: one
  // extra option starts a new pick from there
  const REPICK = "band:repick";
  const bandCommitted = $derived(!bandPick && !!activeOffer && activeOffer.mode === "band");

  const applyView = (key: string) => {
    if (key === REPICK) {
      bandPick = true;
      return;
    }
    const v = key.startsWith("s:") ? suggestions.find((s) => s.id === key.slice(2)) : undefined;
    if (v) return pick({ suggestion: v.id! }, v);
    const o = offers.find((x) => yViewKey(x) === key);
    if (!o) return;
    const a = yViewAction(o);
    if (a.kind === "band") bandPick = true; // a select picks, it cannot toggle: Esc cancels the pick
    else if (a.kind === "indexed") pickIndexed(a.baseline);
    else pick(a.body, a.optimistic);
  };
</script>

{#if data && (data.kind === "time" || offers.length > 0)}
  <div class="simple-toolbar" data-simple-toolbar role="group" aria-label="Panel controls">
    {#if data.kind === "time"}
      <span class="presets" role="group" aria-label="Time range">
        {#each RANGE_PRESETS as preset}
          <button type="button" disabled={rangeBusy} onclick={() => pickPreset(preset)}>{preset}</button>
        {/each}
      </span>
      <RangeStatus {viewport} {previewRender} {rangeBusy} {rangeError} {keepThisRange} />
    {/if}
    <span class="spacer"></span>
    {#if offers.length > 0}
      <select aria-label="Y-axis view" value={yValue} title={activeOffer?.title ?? ""} disabled={idxBusy}
        onchange={(e) => {
          applyView(e.currentTarget.value);
          e.currentTarget.value = yValue; // the panel's state decides what is shown (a failed pick shows the old view)
        }}>
        {#each offers as o (yViewKey(o))}
          <option value={yViewKey(o)} disabled={!o.enabled} title={o.title}>{o.label}{o.suggest ? " (suggested)" : ""}</option>
        {/each}
        {#if bandCommitted}<option value={REPICK}>band… again</option>{/if}
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
      <select aria-label="Data view" value={viewNow ?? ""}
        title={viewNow && viewNow === data.filter.default ? `Claude's pick: ${data.filter.reason}` : ""} onchange={(e) => pickView(e.currentTarget.value as DataViewName)}>
        {#each data.filter.offered as v (v)}
          <option value={v} title={v === data.filter.default ? `Claude's pick: ${data.filter.reason}` : ""}
          >{VIEW_LABELS[v]}{v === data.filter.default ? " · Claude" : ""}</option>
        {/each}
      </select>
      <span class="hint">{data.filter.filter}; dashed: filter edge (unreliable)</span>
    {/if}
    {#if chips.length > 0}
      <details class="layers">
        <summary aria-label="Reference layers">Layers{onChips ? ` (${onChips})` : ""}</summary>
        <div class="layers-pop">
          <!-- checked is one-way: once the request settles (data reloaded, or failed) put the box back to the real state -->
          {#each chips as c (c.key)}
            <label title={c.title}>
              <input type="checkbox" checked={c.on} disabled={!c.enabled || overlayBusy}
                onchange={async (e) => {
                  const box = e.currentTarget;
                  await toggleOverlay(c.key, box.checked);
                  box.checked = c.on;
                }} />
              {c.label}
            </label>
          {/each}
        </div>
      </details>
    {/if}
  </div>
  {#if data.kind === "time"}<PreviewRender {panel} {previewRender} bind:previewEl />{/if}
{/if}
