<script lang="ts">
  import { setupCanvas } from "../chart/canvas";
  import { colormap } from "../chart/colormap";
  import { minSamples, timeColumns, valueAxis } from "../chart/heatmap";
  import { fmtValue, valueTicks } from "../chart/axis";
  import { bandRects, overlay, qKey, qLabel, QUANTILE_CHOICES, thinColumns } from "../chart/percentiles";
  import { PALETTE, seriesName } from "../chart/toUplot";
  import { fmtRange } from "../lib/format";
  import type { HeatmapPanelData, HeatSeries } from "../lib/api";

  // One facet per series, or all series overlaid when a single quantile is shown (<= 5 series).
  let { data, series, qs, width, height, unit, onRendered, onBrush }: {
    data: HeatmapPanelData; series: HeatSeries[]; qs: number[]; width: number; height: number; unit: string | null;
    onRendered: (ms: number, rects: number) => void;
    onBrush: (b: { x0: number; x1: number; left: number; width: number }) => void;
  } = $props();

  const AXIS_LEFT = 64;
  const AXIS_BOTTOM = 16;
  let canvas = $state<HTMLCanvasElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let drag = $state<{ from: number; to: number } | null>(null);
  const plotW = $derived(Math.max(10, width - AXIS_LEFT));
  const plotH = $derived(Math.max(10, height - AXIS_BOTTOM));
  const overlaid = $derived(overlay(series.length, qs.length));
  const cmap = colormap("viridis");
  const qColor = (q: number) => cmap(0.15 + (0.7 * Math.max(0, QUANTILE_CHOICES.indexOf(q))) / (QUANTILE_CHOICES.length - 1));
  const cols = $derived(timeColumns(data.dataset.start_ms, data.dataset.end_ms, data.effective_step_ms, plotW));
  const axis = $derived(
    valueAxis(
      series.flatMap((s) => [...s.cells.lo, ...Object.values(s.quantiles ?? {}).flatMap((b) => b.lo)]),
      series.flatMap((s) => [...s.cells.hi, ...Object.values(s.quantiles ?? {}).flatMap((b) => b.hi)]),
      plotH, "log",
    ),
  );

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
    const v = (name: string) => css.getPropertyValue(name).trim();
    const k = data.effective_step_ms;
    ctx.save();
    ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--grid");
    ctx.lineWidth = 1;
    // no data at all in a column: hatched, never "zero"
    const have = new Set(series.flatMap((s) => s.ts));
    for (let t = cols.x0Ms + k; t <= cols.x1Ms; t += k) if (!have.has(t)) { const c = cols.col(t); hatch(ctx, c.x, c.w, plotH); }
    let count = 0;
    series.forEach((s, si) => {
      for (const r of bandRects(s, qs, axis, cols.col, plotH)) {
        const color = overlaid ? PALETTE[si % PALETTE.length] : qColor(r.q);
        ctx.globalAlpha = 0.35;
        ctx.fillStyle = color;
        ctx.fillRect(r.x, r.y, Math.max(r.w, 1), Math.max(r.h, 1));
        ctx.globalAlpha = 1;
        ctx.strokeStyle = color;
        ctx.beginPath();
        ctx.moveTo(r.x, r.y + 0.5); ctx.lineTo(r.x + Math.max(r.w, 1), r.y + 0.5);
        ctx.moveTo(r.x, r.y + r.h - 0.5); ctx.lineTo(r.x + Math.max(r.w, 1), r.y + r.h - 0.5);
        ctx.stroke();
        count++;
      }
      // too few observations for q: no band, a faded 3 px marker in that q's lane
      qs.forEach((q, qi) => {
        ctx.globalAlpha = 0.4;
        ctx.fillStyle = v("--muted");
        for (const c of thinColumns(s, q, cols.col)) ctx.fillRect(c.x, plotH - 3 * (qi + 1), Math.max(c.w, 1), 2);
        ctx.globalAlpha = 1;
      });
    });
    // axes
    ctx.fillStyle = v("--muted"); ctx.font = "10px sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (const t of valueTicks(axis)) ctx.fillText(fmtValue(t, unit), -4, plotH - axis.pos(t));
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let i = 0; i <= 4; i++) {
      const x = (plotW * i) / 4;
      ctx.textAlign = i === 4 ? "right" : "center";
      ctx.fillText(new Date(cols.x0Ms + (x / plotW) * cols.spanMs).toISOString().slice(11, 16), x, plotH + 2);
    }
    if (drag) { ctx.fillStyle = "rgba(127,127,127,0.25)"; ctx.fillRect(Math.min(drag.from, drag.to), 0, Math.abs(drag.to - drag.from), plotH); }
    ctx.restore();
    onRendered(performance.now() - t0, count);
  });

  const local = (e: MouseEvent) => {
    const b = canvas!.getBoundingClientRect();
    return { x: e.clientX - b.left - AXIS_LEFT, y: e.clientY - b.top };
  };
  const msAt = (px: number) => cols.x0Ms + (px / plotW) * cols.spanMs;

  function move(e: MouseEvent) {
    const p = local(e);
    if (drag) { drag = { ...drag, to: Math.max(0, Math.min(plotW, p.x)) }; return; }
    const k = data.effective_step_ms;
    const ts = cols.x0Ms + Math.ceil((msAt(p.x) - cols.x0Ms) / k) * k;
    const lines: string[] = [];
    for (const s of series) {
      const i = s.ts.indexOf(ts);
      if (i < 0) continue;
      const n = s.n[i];
      const head = `${series.length > 1 ? seriesName(s.labels) + " · " : ""}n ${Number(n.toPrecision(4))}`;
      const parts = qs.map((q) => {
        const b = s.quantiles?.[qKey(q)];
        const j = b ? b.ts.indexOf(ts) : -1;
        if (j < 0) return `${qLabel(q)}: n ${Number(n.toPrecision(3))} < ${minSamples(q)}, not shown`;
        const edge = (x: number | null, inf: string) => (x === null ? inf : fmtValue(x, unit));
        return `${qLabel(q)}: (${edge(b!.lo[j], "−∞")}, ${edge(b!.hi[j], "+∞")}]`;
      });
      lines.push([head, ...parts].join(" · "));
    }
    tip = lines.length ? { x: p.x + AXIS_LEFT + 8, y: p.y + 8, text: `${fmtRange(ts - k, ts)}\n${lines.join("\n")}` } : null;
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
  <div class="facet-label">
    {#if overlaid}
      {#each series as s, i (s.id)}<span class="sw" style="background: {PALETTE[i % PALETTE.length]}"></span>{seriesName(s.labels)}{" "}{/each}
      · {qLabel(qs[0])}
    {:else}
      {seriesName(series[0].labels)}
    {/if}
  </div>
  <canvas
    bind:this={canvas}
    class="percentiles"
    onmousedown={(e) => { const p = local(e); drag = { from: p.x, to: p.x }; }}
    onmousemove={move}
    onmouseup={up}
    onmouseleave={() => { tip = null; }}
  ></canvas>
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px; white-space: pre">{tip.text}</div>{/if}
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .sw { display: inline-block; width: 10px; height: 10px; margin: 0 3px 0 6px; border-radius: 2px; }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; z-index: 5; }
</style>
