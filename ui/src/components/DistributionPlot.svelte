<script lang="ts">
  import { PALETTE, seriesName } from "../chart/toUplot";
  import { bars, ecdf, maxEcdfGapAtEdges, type BarMode } from "../chart/distribution";
  import { setupCanvas } from "../chart/canvas";
  import { cellSpan, valueAxis } from "../chart/heatmap";
  import { fmtValue, valueTicks } from "../chart/axis";
  import { fmtRange } from "../lib/format";
  import type { HistogramPanelData, WindowHist } from "../lib/api";

  let { data, series, width, unit, onRendered }: {
    data: HistogramPanelData;
    series: HistogramPanelData["series"][number];
    width: number;
    unit: string | null;
    onRendered: (ms: number, points: number) => void;
  } = $props();

  const HEIGHT = 220;
  const AXIS_LEFT = 40;
  const AXIS_BOTTOM = 28;
  let canvas = $state<HTMLCanvasElement | null>(null);
  let view = $state<"histogram" | "ecdf">("histogram");
  let userMode = $state<BarMode | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let spans: { lo: number | null; hi: number | null; x0: number; x1: number }[][] = [];

  const windows = $derived(series.windows);
  const multi = $derived(windows.length > 1);
  // counts are not comparable across windows with different n: default to share per decade
  const mode = $derived<BarMode>(multi && (userMode === null || userMode === "count") ? "density" : (userMode ?? "count"));
  const nMin = $derived(data.dataset.n_min ?? 0);
  const plotW = $derived(Math.max(10, width - AXIS_LEFT));
  const plotH = HEIGHT - AXIS_BOTTOM;

  const gap = $derived(multi ? maxEcdfGapAtEdges(windows[0], windows[1]) : null);

  $effect(() => {
    view = data.mark;
  });

  $effect(() => {
    const el = canvas;
    if (!el) return;
    const t0 = performance.now();
    const ctx = setupCanvas(el, width, HEIGHT);
    if (!ctx) return;
    const css = getComputedStyle(el);
    const v = (name: string) => css.getPropertyValue(name).trim();
    const lo = windows.flatMap((w) => w.lo);
    const hi = windows.flatMap((w) => w.hi);
    const axis = valueAxis(lo, hi, plotW);
    const barsByWindow = windows.map((w) => bars(w, mode));
    const yMax =
      view === "ecdf"
        ? 1
        : Math.max(1e-12, ...barsByWindow.flatMap((b) => b.map((x) => x.y ?? 0)));
    const y = (f: number) => plotH - (f / yMax) * (plotH - 4);
    ctx.save();
    ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--grid");
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, plotH);
    ctx.lineTo(plotW, plotH);
    ctx.stroke();
    let points = 0;
    spans = [];
    windows.forEach((w, k) => {
      const color = PALETTE[k % PALETTE.length];
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1.5;
      if (view === "histogram") {
        const list = barsByWindow[k];
        spans[k] = list.map((b) => {
          const [x0, x1] = cellSpan(axis, b.lo, b.hi);
          return { lo: b.lo, hi: b.hi, x0, x1 };
        });
        list.forEach((b, i) => {
          if (b.y === null) return;
          points++;
          const { x0, x1 } = spans[k][i];
          const top = y(b.y);
          if (k === 0) {
            ctx.globalAlpha = 0.25;
            ctx.fillRect(x0, top, Math.max(x1 - x0, 1), plotH - top);
            ctx.globalAlpha = 1;
          }
          ctx.strokeRect(x0 + 0.5, top, Math.max(x1 - x0 - 1, 1), plotH - top);
        });
      } else {
        const steps = ecdf(w);
        spans[k] = steps.map((s) => {
          const [x0, x1] = cellSpan(axis, s.lo, s.hi);
          return { lo: s.lo, hi: s.hi, x0, x1 };
        });
        steps.forEach((s, i) => {
          points++;
          const { x0, x1 } = spans[k][i];
          // inside a bucket the ECDF is only known to lie in [F(lo), F(hi)]: a box, never a line
          ctx.globalAlpha = 0.15;
          ctx.fillRect(x0, y(s.f1), Math.max(x1 - x0, 1), y(s.f0) - y(s.f1));
          ctx.globalAlpha = 1;
          ctx.beginPath();
          ctx.arc(x1, y(s.f1), 2.5, 0, 2 * Math.PI); // exact at the bucket edge
          ctx.fill();
          ctx.beginPath();
          ctx.moveTo(x1, y(s.f1));
          ctx.lineTo(spans[k][i + 1]?.x0 ?? plotW, y(s.f1)); // exact until the next bucket starts
          ctx.stroke();
        });
      }
    });
    // axes
    ctx.fillStyle = v("--muted");
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const t of valueTicks(axis)) ctx.fillText(fmtValue(t, unit), axis.pos(t), plotH + 3);
    if (axis.over) ctx.fillText(`> ${fmtValue(axis.max, unit)}`, plotW - 14, plotH + 14);
    if (axis.under) ctx.fillText(`≤ ${fmtValue(axis.min, unit)}`, 14, plotH + 14);
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const yTicks = view === "ecdf" ? [0, 0.5, 1] : [0, yMax / 2, yMax];
    for (const t of yTicks) {
      const label = view === "ecdf" ? `${Math.round(t * 100)}%` : mode === "count" ? String(Number(t.toPrecision(3))) : t.toPrecision(2);
      ctx.fillText(label, -4, y(t));
    }
    ctx.restore();
    onRendered(performance.now() - t0, points);
  });

  const local = (e: MouseEvent) => {
    const b = canvas!.getBoundingClientRect();
    return { x: e.clientX - b.left - AXIS_LEFT, y: e.clientY - b.top };
  };

  const share = (w: WindowHist, i: number) => {
    const t = w.c.reduce((a, b) => a + b, 0);
    return t > 0 ? (100 * w.c[i]) / t : 0;
  };

  function move(e: MouseEvent) {
    const p = local(e);
    const lines: string[] = [];
    windows.forEach((w, k) => {
      const list = spans[k] ?? [];
      const bi = list.findIndex((s) => p.x >= s.x0 && p.x < Math.max(s.x1, s.x0 + 1));
      if (bi < 0) return;
      // bars()/ecdf() list buckets ordered by upper edge: find the matching source bucket
      const s = list[bi];
      const idx = w.hi.findIndex((h, i) => h === s.hi && w.lo[i] === s.lo);
      if (idx < 0) return;
      const edge = (x: number | null, inf: string) => (x === null ? inf : fmtValue(x, unit));
      lines.push(
        `${w.label || `window ${k + 1}`}: (${edge(s.lo, "−∞")}, ${edge(s.hi, "+∞")}] · count ${Number(w.c[idx].toPrecision(4))} · ${share(w, idx).toPrecision(3)}%`,
      );
    });
    tip = lines.length ? { x: p.x + AXIS_LEFT + 8, y: p.y + 8, text: lines.join("\n") } : null;
  }
</script>

<div class="facet">
  <div class="facet-label">{seriesName(series.labels)}</div>
  <div class="dist-controls">
    <button type="button" class:on={view === "histogram"} onclick={() => (view = "histogram")}>histogram</button>
    <button type="button" class:on={view === "ecdf"} onclick={() => (view = "ecdf")}>ECDF</button>
    {#if view === "histogram"}
      {#each ["count", "share", "density"] as m (m)}
        <button
          type="button"
          class:on={mode === m}
          disabled={m === "count" && multi}
          title={m === "count" && multi ? "windows have different n: counts are not comparable" : ""}
          onclick={() => (userMode = m as BarMode)}>{m}</button
        >
      {/each}
    {/if}
  </div>
  <canvas bind:this={canvas} class="distribution" onmousemove={move} onmouseleave={() => (tip = null)}></canvas>
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px; white-space: pre">{tip.text}</div>{/if}
  <div class="legend">
    {#each windows as w, k (k)}
      <div>
        <span class="sw" style="background: {PALETTE[k % PALETTE.length]}"></span>
        {w.label || `window ${k + 1}`} {fmtRange(w.start_ms, w.end_ms)} · n = {Number(w.n.toPrecision(4))} ({w.columns} steps){#if w.n > 0 && w.n < nMin}
          · low n{/if}
      </div>
    {/each}
    {#if gap}
      <div>max |ΔF| at bucket edges = {gap.gap.toFixed(2)} at {fmtValue(gap.at, unit)} (lower bound, edges where both ECDFs are exact)</div>
    {/if}
    {#if data.value_merge > 1}<div>{data.value_merge} source buckets per bar</div>{/if}
  </div>
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .dist-controls { display: flex; gap: 4px; margin: 2px 0; }
  .dist-controls button { font-size: 11px; padding: 1px 6px; border: 1px solid var(--border); background: none; color: var(--muted); border-radius: 4px; cursor: pointer; }
  .dist-controls button.on { color: var(--fg); border-color: var(--fg); }
  .dist-controls button:disabled { opacity: 0.4; cursor: not-allowed; }
  .sw { display: inline-block; width: 10px; height: 10px; margin-right: 4px; border-radius: 2px; }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; z-index: 5; }
</style>
