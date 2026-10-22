<script lang="ts">
  import { setupCanvas } from "../chart/canvas";
  import { colormap } from "../chart/colormap";
  import { fmtPeriod, periodTicks } from "../chart/period";
  import { layoutSpectrogram } from "../chart/spectrogram";
  import { seriesName } from "../chart/toUplot";
  import { shortNamer } from "../chart/seriesNames";
  import { fmtRange } from "../lib/format";
  import { drawAnnotations, drawOps, readAnnotationColors } from "../chart/annotations";
  import type { Annotation, SpectrogramPanelData } from "../lib/api";

  let { data, series, width, height, annotations, onRendered, onBrush }: {
    data: SpectrogramPanelData; series: SpectrogramPanelData["series"][number]; width: number; height: number;
    annotations: Annotation[]; // this panel's: only the time-keyed ones (event, region) have a place on a period-by-time plane
    onRendered: (ms: number, cells: number) => void;
    onBrush: (b: { x0: number; x1: number; left: number; width: number }) => void;
  } = $props();

  const short = $derived(shortNamer(data.series.map((s) => s.labels)));
  const AXIS_LEFT = 64, AXIS_BOTTOM = 16;
  let canvas = $state<HTMLCanvasElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let drag = $state<{ from: number; to: number } | null>(null);
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

  const msAt = (px: number) => startMs + (px / plotW) * (endMs - startMs);

  // drawAnnotations works in canvas pixels against a uPlot-shaped plot: present this canvas as one
  function drawTimeAnnotations(ctx: CanvasRenderingContext2D, el: HTMLCanvasElement) {
    const dpr = window.devicePixelRatio || 1;
    const ops = drawOps(annotations, { min: startMs / 1000, max: endMs / 1000 }, { min: -Infinity, max: Infinity })
      .filter((op) => op.type === "vline" || op.type === "xband");
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    drawAnnotations({
      ctx,
      bbox: { left: AXIS_LEFT * dpr, top: 0, width: plotW * dpr, height: plotH * dpr },
      valToPos: (sec) => (AXIS_LEFT + ((sec * 1000 - startMs) / (endMs - startMs)) * plotW) * dpr,
    }, ops, readAnnotationColors(el), dpr);
    ctx.restore();
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
    if (drag) { ctx.fillStyle = "rgba(127,127,127,0.25)"; ctx.fillRect(Math.min(drag.from, drag.to), 0, Math.abs(drag.to - drag.from), plotH); }
    ctx.restore();
    drawTimeAnnotations(ctx, el);
    onRendered(performance.now() - t0, l.rects.length);
  });

  function move(e: MouseEvent) {
    const b = canvas!.getBoundingClientRect();
    const px = e.clientX - b.left - AXIS_LEFT, py = e.clientY - b.top;
    if (drag) { drag = { ...drag, to: Math.max(0, Math.min(plotW, px)) }; return; }
    const hit = layout?.rects.findLast((r) => px >= r.x && px < r.x + r.w && py >= r.y && py < r.y + r.h);
    if (!hit) { tip = null; return; }
    const lvl = series.level[hit.col];
    tip = {
      x: px + AXIS_LEFT + 8, y: py + 8,
      text: `${fmtRange(series.ts[hit.col] - data.segment_ms / 2, series.ts[hit.col] + data.segment_ms / 2)} · period ${fmtPeriod(series.rows.lo_s[hit.row])}–${fmtPeriod(series.rows.hi_s[hit.row])} · power ${hit.t}${lvl != null && hit.t < lvl ? " (not significant)" : ""}`,
    };
  }
  function up() {
    if (drag && Math.abs(drag.to - drag.from) >= 3) {
      const a = Math.min(drag.from, drag.to), b = Math.max(drag.from, drag.to);
      onBrush({ x0: msAt(a) / 1000, x1: msAt(b) / 1000, left: a + AXIS_LEFT, width: b - a });
    }
    drag = null;
  }
</script>

<div class="facet">
  <div class="facet-label" title={seriesName(series.labels)}>{short(series.labels)}</div>
  <canvas bind:this={canvas} class="spectrogram"
    onmousedown={(e) => { const b = canvas!.getBoundingClientRect(); const x = Math.max(0, Math.min(plotW, e.clientX - b.left - AXIS_LEFT)); drag = { from: x, to: x }; }}
    onmousemove={move} onmouseup={up} onmouseleave={() => (tip = null)}></canvas>
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px">{tip.text}</div>{/if}
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; white-space: nowrap; z-index: 5; }
</style>
