<script lang="ts">
  import { getContext } from "svelte";
  import { GROUP_GUTTER_PX } from "../lib/groupLink.svelte";
  import { setupCanvas } from "../chart/canvas";
  import { timeColumns } from "../chart/heatmap";
  import type { HeatmapPanelData, HeatSeries } from "../lib/api";

  // Observations per column, aligned under a percentile/heatmap facet: a percentile line gives
  // every column equal visual weight, so this shows how much traffic each one rests on.
  let { data, series, width }: { data: HeatmapPanelData; series: HeatSeries[]; width: number } = $props();

  // a panel group (czt.3) widens the gutter so every role's x axis starts at the same pixel
  const AXIS_LEFT = getContext("panelGroup") ? GROUP_GUTTER_PX : 64;
  const HEIGHT = 26;
  let canvas = $state<HTMLCanvasElement | null>(null);
  const plotW = $derived(Math.max(10, width - AXIS_LEFT));
  const nMin = $derived(data.dataset.n_min ?? 0);

  // total observations per column across the shown series (counts are additive)
  const totals = $derived.by(() => {
    const m = new Map<number, number>();
    for (const s of series) s.ts.forEach((t, i) => m.set(t, (m.get(t) ?? 0) + s.n[i]));
    return m;
  });
  const nMax = $derived(Math.max(1, ...totals.values()));

  $effect(() => {
    const el = canvas;
    if (!el) return;
    const ctx = setupCanvas(el, width, HEIGHT);
    if (!ctx) return;
    const css = getComputedStyle(el);
    const { col } = timeColumns(data.dataset.start_ms, data.dataset.end_ms, data.effective_step_ms, plotW);
    ctx.save();
    ctx.translate(AXIS_LEFT, 0);
    for (const [t, n] of totals) {
      const c = col(t);
      const h = Math.max(n > 0 ? 1 : 0, (n / nMax) * (HEIGHT - 4));
      ctx.globalAlpha = n < nMin ? 0.4 : 0.9; // too few observations: muted, like the faded bands
      ctx.fillStyle = css.getPropertyValue("--muted").trim();
      ctx.fillRect(c.x, HEIGHT - h, Math.max(c.w - 0.5, 1), h);
    }
    ctx.restore();
  });
</script>

<div class="count-strip">
  <canvas bind:this={canvas}></canvas>
  <span class="label" style="margin-left: {AXIS_LEFT + 4}px">n per {Math.round(data.effective_step_ms / 1000)}s column · max {Number(nMax.toPrecision(3))}{nMin ? ` · muted: n < ${nMin}` : ""}</span>
</div>

<style>
  .count-strip { display: flex; flex-direction: column; margin-bottom: 4px; }
  .label { font-size: 10px; color: var(--muted); margin-top: 2px; white-space: nowrap; }
</style>
