<script lang="ts">
  import { setupCanvas } from "../chart/canvas";
  import { colormap } from "../chart/colormap";
  import { fmtPeriod, periodTicks } from "../chart/period";
  import { layoutSpectrogram } from "../chart/spectrogram";
  import { seriesName } from "../chart/toUplot";
  import { fmtRange } from "../lib/format";
  import type { SpectrogramPanelData } from "../lib/api";

  let { data, series, width, height, onRendered }: {
    data: SpectrogramPanelData; series: SpectrogramPanelData["series"][number]; width: number; height: number;
    onRendered: (ms: number, cells: number) => void;
  } = $props();

  const AXIS_LEFT = 64, AXIS_BOTTOM = 16;
  let canvas = $state<HTMLCanvasElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let layout: ReturnType<typeof layoutSpectrogram> | null = null;
  const plotW = $derived(Math.max(10, width - AXIS_LEFT));
  const plotH = $derived(Math.max(10, height - AXIS_BOTTOM));
  const cmap = colormap("viridis");
  const startMs = $derived(data.dataset.start_ms);
  const endMs = $derived(data.dataset.end_ms);

  function hatch(ctx: CanvasRenderingContext2D, x: number, w: number, h: number) {
    ctx.save(); ctx.beginPath(); ctx.rect(x, 0, w, h); ctx.clip(); ctx.beginPath();
    for (let d = -h; d < w; d += 6) { ctx.moveTo(x + d, h); ctx.lineTo(x + d + h, 0); }
    ctx.stroke(); ctx.restore();
  }

  $effect(() => {
    const el = canvas;
    if (!el) return;
    const t0 = performance.now();
    const ctx = setupCanvas(el, width, height);
    if (!ctx) return;
    const css = getComputedStyle(el);
    const v = (n: string) => css.getPropertyValue(n).trim();
    const l = layoutSpectrogram(series, { width: plotW, height: plotH, startMs, endMs, hopMs: data.hop_ms });
    layout = l;
    ctx.save(); ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--grid");
    for (const m of l.missing) hatch(ctx, m.x, m.w, plotH); // window with < 50% coverage: no spectrum, never zero
    for (const r of l.rects) {
      ctx.globalAlpha = r.faint ? 0.35 : 1; // below the 1% false-alarm level: not significant
      ctx.fillStyle = cmap(r.t);
      ctx.fillRect(r.x, r.y, Math.max(r.w, 1), Math.max(r.h, 1));
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = v("--muted"); ctx.font = "10px sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    const lo = Math.min(...series.rows.lo_s), hi = Math.max(...series.rows.hi_s);
    for (const p of periodTicks(lo, hi)) ctx.fillText(fmtPeriod(p), -4, plotH - l.axis.pos(p));
    ctx.textBaseline = "top";
    for (let i = 0; i <= 4; i++) {
      const x = (plotW * i) / 4;
      ctx.textAlign = i === 4 ? "right" : "center";
      ctx.fillText(new Date(startMs + (x / plotW) * (endMs - startMs)).toISOString().slice(11, 16), x, plotH + 2);
    }
    ctx.restore();
    onRendered(performance.now() - t0, l.rects.length);
  });

  function move(e: MouseEvent) {
    const b = canvas!.getBoundingClientRect();
    const px = e.clientX - b.left - AXIS_LEFT, py = e.clientY - b.top;
    const hit = layout?.rects.findLast((r) => px >= r.x && px < r.x + r.w && py >= r.y && py < r.y + r.h);
    if (!hit) { tip = null; return; }
    const lvl = series.level[hit.col];
    tip = {
      x: px + AXIS_LEFT + 8, y: py + 8,
      text: `${fmtRange(series.ts[hit.col] - data.segment_ms / 2, series.ts[hit.col] + data.segment_ms / 2)} · period ${fmtPeriod(series.rows.lo_s[hit.row])}–${fmtPeriod(series.rows.hi_s[hit.row])} · power ${hit.t}${lvl != null && hit.t < lvl ? " (not significant)" : ""}`,
    };
  }
</script>

<div class="facet">
  <div class="facet-label">{seriesName(series.labels)}</div>
  <canvas bind:this={canvas} class="spectrogram" onmousemove={move} onmouseleave={() => (tip = null)}></canvas>
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px">{tip.text}</div>{/if}
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; white-space: nowrap; z-index: 5; }
</style>
