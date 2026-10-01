<script lang="ts">
  import { colormap, type ColormapName } from "../chart/colormap";
  import { hitTest, layoutHeatmap, quantileCells, STRIP_PX, timeAt, type HeatLayout } from "../chart/heatmap";
  import { fmtValue, valueTicks } from "../chart/axis";
  import { seriesName } from "../chart/toUplot";
  import { fmtRange } from "../lib/format";
  import type { HeatmapPanelData, HeatSeries } from "../lib/api";

  let { data, series, width, height, unit, color = "count", cmapName = "viridis", overlayQ = null, onRendered, onBrush }: {
    data: HeatmapPanelData; series: HeatSeries; width: number; height: number; unit: string | null;
    color?: "count" | "density";
    cmapName?: ColormapName;
    overlayQ?: number | null; // outline the bucket holding this quantile, where n is enough
    onRendered: (ms: number, cells: number) => void;
    onBrush: (b: { x0: number; x1: number; left: number; width: number }) => void;
  } = $props();

  const AXIS_LEFT = 64;
  const AXIS_BOTTOM = 16;
  let canvas = $state<HTMLCanvasElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let drag = $state<{ from: number; to: number } | null>(null);
  let layout: HeatLayout | null = null;
  const plotW = $derived(Math.max(10, width - AXIS_LEFT));
  const plotH = $derived(Math.max(10, height - AXIS_BOTTOM));
  const cmap = $derived(colormap(cmapName));

  function hatch(ctx: CanvasRenderingContext2D, x: number, w: number, h: number) {
    ctx.save(); ctx.beginPath(); ctx.rect(x, 0, w, h); ctx.clip(); ctx.beginPath();
    for (let d = -h; d < w; d += 6) { ctx.moveTo(x + d, h); ctx.lineTo(x + d + h, 0); }
    ctx.stroke(); ctx.restore();
  }

  $effect(() => {
    const el = canvas;
    if (!el) return;
    const t0 = performance.now();
    const dpr = window.devicePixelRatio || 1;
    el.width = Math.round(width * dpr); el.height = Math.round(height * dpr);
    el.style.width = `${width}px`; el.style.height = `${height}px`;
    const ctx = el.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const css = getComputedStyle(el);
    const v = (name: string) => css.getPropertyValue(name).trim();
    const l = layoutHeatmap(series, {
      width: plotW, height: plotH, startMs: data.dataset.start_ms, endMs: data.dataset.end_ms,
      stepMs: data.effective_step_ms, nMin: data.dataset.n_min ?? 0, color,
    });
    layout = l;
    ctx.save(); ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--grid"); ctx.lineWidth = 1;
    for (const m of l.missing) hatch(ctx, m.x, m.w, plotH); // no data: hatched, never "zero"
    for (const r of l.rects) {
      ctx.globalAlpha = r.lowN ? 0.45 : 1;
      ctx.fillStyle = cmap(r.t);
      ctx.fillRect(r.x, r.y, Math.max(r.w, 1), Math.max(r.h, 1));
    }
    ctx.globalAlpha = 1;
    if (overlayQ !== null) {
      // the quantile's source bucket, outlined: an honest range, never an interpolated line
      for (const qc of quantileCells(series, overlayQ)) {
        const r = l.rects.find((x) => x.cell === qc.cell);
        if (!r) continue;
        ctx.lineWidth = 2; ctx.strokeStyle = "#000";
        ctx.strokeRect(r.x + 1, r.y + 1, Math.max(r.w - 2, 1), Math.max(r.h - 2, 1));
        ctx.lineWidth = 1; ctx.strokeStyle = "#fff";
        ctx.strokeRect(r.x + 1.5, r.y + 1.5, Math.max(r.w - 3, 1), Math.max(r.h - 3, 1));
      }
    }
    ctx.fillStyle = v("--warn");
    for (const m of l.lowN) ctx.fillRect(m.x, plotH - 3, m.w, 3);
    // value axis
    ctx.fillStyle = v("--muted"); ctx.font = "10px sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (const t of valueTicks(l.axis)) {
      const y = plotH - l.axis.pos(t);
      // a tick too close to an open-bucket strip label would overprint it
      if ((l.axis.over && y < STRIP_PX + 8) || (l.axis.under && y > plotH - STRIP_PX - 8)) continue;
      ctx.fillText(fmtValue(t, unit), -4, y);
    }
    if (l.axis.over) ctx.fillText(`> ${fmtValue(l.axis.max, unit)}`, -4, STRIP_PX / 2);
    if (l.axis.under) ctx.fillText(`≤ ${fmtValue(l.axis.min, unit)}`, -4, plotH - STRIP_PX / 2);
    // time axis: 5 ticks
    ctx.textBaseline = "top";
    for (let i = 0; i <= 4; i++) {
      const x = (plotW * i) / 4;
      ctx.textAlign = i === 4 ? "right" : "center"; // keep the last label inside the canvas
      ctx.fillText(new Date(timeAt(l, x)).toISOString().slice(11, 16), x, plotH + 2);
    }
    if (drag) { ctx.fillStyle = "rgba(127,127,127,0.25)"; ctx.fillRect(Math.min(drag.from, drag.to), 0, Math.abs(drag.to - drag.from), plotH); }
    ctx.restore();
    onRendered(performance.now() - t0, l.rects.length);
  });

  const local = (e: MouseEvent) => {
    const b = canvas!.getBoundingClientRect();
    return { x: e.clientX - b.left - AXIS_LEFT, y: e.clientY - b.top };
  };
  function move(e: MouseEvent) {
    const p = local(e);
    if (drag) { drag = { ...drag, to: Math.max(0, Math.min(plotW, p.x)) }; return; }
    const i = layout ? hitTest(layout, p.x, p.y) : null;
    if (i === null || !layout) { tip = null; return; }
    const cell = layout.rects[i].cell;
    const ts = series.cells.ts[cell], lo = series.cells.lo[cell], hi = series.cells.hi[cell], c = series.cells.c[cell];
    const n = layout.nAt.get(ts) ?? 0;
    const edge = (x: number | null, inf: string) => (x === null ? inf : fmtValue(x, unit));
    tip = {
      x: p.x + AXIS_LEFT + 8, y: p.y + 8,
      text: `${fmtRange(ts - data.effective_step_ms, ts)} · (${edge(lo, "−∞")}, ${edge(hi, "+∞")}] · count ${Number(c.toPrecision(4))} · n ${Number(n.toPrecision(4))}${n > 0 ? ` (${((100 * c) / n).toPrecision(3)}%)` : ""}${n > 0 && n < (data.dataset.n_min ?? 0) ? " · low n" : ""}`,
    };
  }
  function up() {
    if (drag && layout && Math.abs(drag.to - drag.from) >= 3) {
      const a = Math.min(drag.from, drag.to), b = Math.max(drag.from, drag.to);
      onBrush({ x0: timeAt(layout, a) / 1000, x1: timeAt(layout, b) / 1000, left: a + AXIS_LEFT, width: b - a });
    }
    drag = null;
  }
</script>

<div class="facet">
  <div class="facet-label">{seriesName(series.labels)}</div>
  <canvas
    bind:this={canvas}
    class="heatmap"
    onmousedown={(e) => { const p = local(e); drag = { from: p.x, to: p.x }; }}
    onmousemove={move}
    onmouseup={up}
    onmouseleave={() => { tip = null; }}
  ></canvas>
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px">{tip.text}</div>{/if}
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; white-space: nowrap; z-index: 5; }
</style>
