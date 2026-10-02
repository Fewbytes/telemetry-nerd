<script lang="ts">
  import { setupCanvas } from "../chart/canvas";
  import { colormap, type ColormapName } from "../chart/colormap";
  import { cellSpan, hitTest, layoutHeatmap, STRIP_PX, timeAt, timeColumns, type HeatLayout } from "../chart/heatmap";
  import { fmtValue, valueTicks } from "../chart/axis";
  import { drawRug, hitRug, rugCells, rugHeight, rugHint, type RugCell } from "../chart/rug";
  import { rgba, seriesName } from "../chart/toUplot";
  import { fmtRange } from "../lib/format";
  import { focusRects } from "../chart/focus";
  import type { HeatmapPanelData, HeatSeries, Where } from "../lib/api";

  let { data, series, width, height, unit, color = "count", cmapName = "viridis", overlayQ = null, focusWhere = null, onRendered, onBrush }: {
    data: HeatmapPanelData; series: HeatSeries; width: number; height: number; unit: string | null;
    color?: "count" | "density";
    cmapName?: ColormapName;
    overlayQ?: number | null; // outline the bucket holding this quantile, where n is enough
    focusWhere?: Where | null; // footer note under the pointer: tint the spans it covers
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

  function dots(ctx: CanvasRenderingContext2D, x: number, w: number, h: number) {
    ctx.save(); ctx.fillStyle = ctx.strokeStyle as string;
    for (let yy = 3; yy < h; yy += 6) for (let xx = x + 3; xx < x + w; xx += 6) ctx.fillRect(xx - 0.75, yy - 0.75, 1.5, 1.5);
    ctx.restore();
  }

  let rugEl = $state<HTMLCanvasElement | null>(null);
  let rugCellsNow: RugCell[] = [];
  let rugTip = $state<{ x: number; y: number; text: string } | null>(null);
  $effect(() => {
    const el = rugEl;
    const st = series.state;
    if (!el || !st) return;
    const ctx = setupCanvas(el, plotW, rugHeight(1));
    if (!ctx) return;
    const css = getComputedStyle(el);
    const grey = css.getPropertyValue("--muted").trim();
    const fg = css.getPropertyValue("--fg").trim();
    const { col } = timeColumns(data.dataset.start_ms, data.dataset.end_ms, data.effective_step_ms, plotW);
    const toX = (ms: number) => { const c = col(ms); return c.x + c.w; };
    rugCellsNow = rugCells([st], data.effective_step_ms, toX);
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, plotW, rugHeight(1)); ctx.clip();
    drawRug(ctx, rugCellsNow, { tint: () => rgba(fg, 0.12), grey, line: grey });
    ctx.restore();
  });
  function onRugMove(e: MouseEvent) {
    const st = series.state;
    const c = st ? hitRug(rugCellsNow, e.offsetX, e.offsetY) : null;
    if (!c || !st) { rugTip = null; return; }
    rugTip = { x: e.offsetX + AXIS_LEFT + 8, y: (rugEl?.offsetTop ?? 0) + e.offsetY + 12, text: rugHint(c, st, data.effective_step_ms, seriesName(series.labels), data.dataset.resolution_ms) };
  }

  $effect(() => {
    const el = canvas;
    if (!el) return;
    const t0 = performance.now();
    const ctx = setupCanvas(el, width, height);
    if (!ctx) return;
    const css = getComputedStyle(el);
    const v = (name: string) => css.getPropertyValue(name).trim();
    const l = layoutHeatmap(series, {
      width: plotW, height: plotH, startMs: data.dataset.start_ms, endMs: data.dataset.end_ms,
      stepMs: data.effective_step_ms, nMin: data.dataset.n_min ?? 0, color,
    });
    layout = l;
    ctx.save(); ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--muted"); ctx.lineWidth = 1; // texture carries meaning: >= 3:1
    for (const m of l.missing) m.kind === "unknown" ? hatch(ctx, m.x, m.w, plotH) : dots(ctx, m.x, m.w, plotH); // never "zero"
    for (const r of l.rects) {
      ctx.globalAlpha = r.lowN ? 0.45 : 1;
      ctx.fillStyle = cmap(r.t);
      ctx.fillRect(r.x, r.y, Math.max(r.w, 1), Math.max(r.h, 1));
    }
    ctx.globalAlpha = 1;
    if (overlayQ !== null) {
      // the quantile's source bucket, outlined: an honest range, never an interpolated line
      const band = series.quantiles?.[String(overlayQ)];
      const { col } = timeColumns(data.dataset.start_ms, data.dataset.end_ms, data.effective_step_ms, plotW);
      band?.ts.forEach((ts, i) => {
        const c = col(ts);
        const [p0, p1] = cellSpan(l.axis, band.lo[i], band.hi[i]);
        const x = c.x, y = plotH - p1, w = c.w, h = p1 - p0;
        ctx.lineWidth = 2; ctx.strokeStyle = "#000";
        ctx.strokeRect(x + 1, y + 1, Math.max(w - 2, 1), Math.max(h - 2, 1));
        ctx.lineWidth = 1; ctx.strokeStyle = "#fff";
        ctx.strokeRect(x + 1.5, y + 1.5, Math.max(w - 3, 1), Math.max(h - 3, 1));
      });
    }
    if (focusWhere) {
      const { col } = timeColumns(data.dataset.start_ms, data.dataset.end_ms, data.effective_step_ms, plotW);
      ctx.save(); ctx.fillStyle = v("--warn"); ctx.globalAlpha = 0.15;
      for (const f of focusRects(focusWhere, (ms) => { const c = col(ms); return c.x + c.w; }, 0, plotW)) ctx.fillRect(f.x, 0, f.w, plotH);
      ctx.restore();
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
  {#if series.state}
    <canvas class="rug" bind:this={rugEl} data-rug aria-label="Coverage rug: where data is missing"
      style="margin-left: {AXIS_LEFT}px" onmousemove={onRugMove} onmouseleave={() => (rugTip = null)}></canvas>
    {#if rugTip}<div class="rug-tip" style="left: {rugTip.x}px; top: {rugTip.y}px">{rugTip.text}</div>{/if}
  {/if}
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px">{tip.text}</div>{/if}
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .rug { display: block; }
  .rug-tip { position: absolute; white-space: pre; font-size: 11px; background: var(--fg); color: var(--bg); padding: 4px 6px; border-radius: 4px; pointer-events: none; z-index: 5; }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; white-space: nowrap; z-index: 5; }
</style>
