<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { flaggedSpans, littlesLegend, ratioRange, seriesTitle, toLittlesUplot, toRatioUplot, verdictText, windowTip } from "../chart/littles";
  import { fmtRatio } from "../chart/indexed";
  import { HIDDEN_SERIES, plotAxes, axisGutterSize } from "../chart/plotKit";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { LittlesPanelData } from "../lib/api";

  let { data, series, width, height = 200, onRendered }: {
    data: LittlesPanelData; series: LittlesPanelData["series"][number]; width: number; height?: number;
    onRendered: (ms: number, points: number) => void;
  } = $props();

  const RATIO_H = 110;
  const L_COLOR = "#0072B2", LW_COLOR = "#E69F00";
  let top = $state<HTMLDivElement | null>(null);
  let strip = $state<HTMLDivElement | null>(null);
  let wrap = $state<HTMLDivElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string; flip: boolean } | null>(null);
  const spans = $derived(flaggedSpans(series));

  function shade(p: uPlot, mode: string) {
    const c = p.ctx;
    c.save();
    for (const s of spans) {
      const x0 = p.valToPos(s.x0, "x", true), x1 = p.valToPos(s.x1, "x", true);
      c.fillStyle = s.verdict === "L_high"
        ? (mode === "dark" ? "rgba(213,94,0,0.20)" : "rgba(213,94,0,0.14)")
        : (mode === "dark" ? "rgba(204,121,167,0.22)" : "rgba(204,121,167,0.16)");
      c.fillRect(x0, p.bbox.top, x1 - x0, p.bbox.height);
    }
    c.restore();
  }

  function hover(p: uPlot) {
    const { left, top: y } = p.cursor;
    const box = wrap;
    if (left == null || y == null || left < 0 || !box) { tip = null; return; }
    const text = windowTip(series, p.posToVal(left, "x"));
    if (!text) { tip = null; return; }
    const o = p.over.getBoundingClientRect(), w = box.getBoundingClientRect();
    const x = o.left - w.left + left, yy = o.top - w.top + y;
    const flip = x > w.width / 2;
    tip = { x: flip ? x - 12 : x + 12, y: yy + 12, text, flip };
  }

  $effect(() => {
    const a = top, b = strip;
    if (!a || !b) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(a, mode);
    const stepped = uPlot.paths.stepped!({ align: 1 });
    const bandFill = (rgb: string) => `rgba(${rgb},${mode === "dark" ? 0.22 : 0.16})`;
    const m = toLittlesUplot(series);
    const r = toRatioUplot(series);
    const gutter = axisGutterSize();
    const cursor = { drag: { x: false, y: false }, points: { show: false }, sync: { key: `littles-${data.panel.id}-${series.id}` } };
    const u1 = new uPlot(
      {
        width, height,
        series: [
          {},
          { ...HIDDEN_SERIES, label: "L lo", paths: stepped },
          { ...HIDDEN_SERIES, label: "L hi", paths: stepped },
          { label: "L", stroke: L_COLOR, width: 1.75, paths: stepped, points: { show: false } },
          { ...HIDDEN_SERIES, label: "λW lo", paths: stepped },
          { ...HIDDEN_SERIES, label: "λW hi", paths: stepped },
          { label: "λ·W", stroke: LW_COLOR, width: 1.75, dash: [6, 3], paths: stepped, points: { show: false } },
        ],
        bands: [
          { ...m.bands[0], fill: bandFill("0,114,178") },
          { ...m.bands[1], fill: bandFill("230,159,0") },
        ],
        scales: { y: { range: (_u, lo, hi) => [Math.min(0, lo ?? 0), (hi ?? 1) * 1.05] } },
        legend: { show: false },
        axes: plotAxes(stroke, grid, { label: "requests in flight", size: gutter }, { show: false }),
        cursor,
        hooks: { drawClear: [(p: uPlot) => shade(p, mode)], setCursor: [hover] },
      },
      m.data, a,
    );
    const u2 = new uPlot(
      {
        width, height: RATIO_H,
        series: [
          {},
          { ...HIDDEN_SERIES, label: "ratio lo", paths: stepped },
          { ...HIDDEN_SERIES, label: "ratio hi", paths: stepped },
          { label: "L ÷ λW", stroke, width: 1.5, paths: stepped, points: { show: false } },
          { label: "1", stroke: grid, width: 1, dash: [4, 3], points: { show: false } },
        ],
        bands: [{ ...r.bands[0], fill: mode === "dark" ? "rgba(170,170,170,0.28)" : "rgba(90,90,90,0.18)" }],
        scales: { y: { distr: 3, log: 2, range: () => ratioRange(r.data) } },
        legend: { show: false },
        axes: plotAxes(stroke, grid, {
          label: "L ÷ λW", size: gutter, splits: () => [0.25, 0.5, 1, 2, 4, 8].filter((v) => { const [a, b] = ratioRange(r.data); return v >= a && v <= b; }),
          values: (_u: uPlot, ts: (number | null)[]) => ts.map((t) => (t == null ? "" : fmtRatio(t))),
        }),
        cursor,
        hooks: { drawClear: [(p: uPlot) => shade(p, mode)], setCursor: [hover] },
      },
      r.data, b,
    );
    onRendered(performance.now() - t0, (m.data[0] as number[]).length * 3);
    return () => { u1.destroy(); u2.destroy(); };
  });
</script>

<div class="littles" data-littles-verdict={series.verdict} data-littles-flagged={spans.length}>
  <div class="verdict">
    {seriesTitle(series)}: <b>{verdictText(series.verdict)}</b>
    <span class="key"><i style="background:{L_COLOR}"></i>L <i class="dash" style="border-color:{LW_COLOR}"></i>λ·W</span>
  </div>
  <div class="plot" bind:this={wrap} role="presentation" onmouseleave={() => (tip = null)}>
    <div bind:this={top}></div>
    <div bind:this={strip}></div>
    {#if tip}
      <div class="littles-tip" class:flip={tip.flip} data-littles-tip style="left:{tip.x}px;top:{tip.y}px">{tip.text}</div>
    {/if}
  </div>
  <div class="legend">{littlesLegend(series, data.window_ms)}</div>
</div>

<style>
  .verdict { font-size: 0.85em; margin: 2px 0; display: flex; gap: 12px; align-items: center; }
  .key { opacity: 0.8; display: inline-flex; gap: 4px; align-items: center; }
  .key i { display: inline-block; width: 14px; height: 2px; }
  .key i.dash { height: 0; border-top: 2px dashed; }
  .plot { position: relative; }
  .littles-tip {
    position: absolute; white-space: pre; font-size: 11px; background: var(--fg); color: var(--bg);
    padding: 4px 6px; border-radius: 4px; pointer-events: none; z-index: 5;
  }
  .littles-tip.flip { transform: translateX(-100%); }
</style>
