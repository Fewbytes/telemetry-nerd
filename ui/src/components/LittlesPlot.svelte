<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { discrepancyText, littlesLegend, promotedSpans, ratioRange, seriesTitle, systematicLabel, toLittlesUplot, toRatioUplot, transientSpans, verdictText, windowTip } from "../chart/littles";
  import { fmtRatio } from "../chart/indexed";
  import { HIDDEN_SERIES, plotAxes, axisGutterSize, tipAt, type HoverTip } from "../chart/plotKit";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { LittlesPanelData } from "../lib/api";
  import ChartTip from "./ChartTip.svelte";

  let { data, series, width, height = 200, onRendered }: {
    data: LittlesPanelData; series: LittlesPanelData["series"][number]; width: number; height?: number;
    onRendered: (ms: number, points: number) => void;
  } = $props();

  const RATIO_H = 150; // the discrepancy strip leads: it is what the check is about
  const L_COLOR = "#0072B2", LW_COLOR = "#E69F00", SYS_COLOR = "#CC79A7";
  let top = $state<HTMLDivElement | null>(null);
  let strip = $state<HTMLDivElement | null>(null);
  let wrap = $state<HTMLDivElement | null>(null);
  let tip = $state<HoverTip | null>(null);
  const spans = $derived(transientSpans(series));
  const promoted = $derived(promotedSpans(series));
  const sysLabel = $derived(systematicLabel(series));
  const warning = $derived(series.common_cause?.warning ?? null);

  /**
   * Transient windows: special cause solid (investigate), common cause hatched (do not chase).
   * Promoted load-peak windows (special cause on evidence of leaving steady state): solid, with a
   * bar along the top; the hover says why.
   */
  function shade(p: uPlot, mode: string) {
    const c = p.ctx;
    c.save();
    const solid = mode === "dark" ? "rgba(213,94,0,0.24)" : "rgba(213,94,0,0.16)";
    for (const s of promoted) {
      const x0 = p.valToPos(s.x0, "x", true), x1 = p.valToPos(s.x1, "x", true);
      if (!spans.some((t) => t.x0 === s.x0)) {
        c.fillStyle = solid;
        c.fillRect(x0, p.bbox.top, x1 - x0, p.bbox.height);
      }
    }
    for (const s of spans) {
      const x0 = p.valToPos(s.x0, "x", true), x1 = p.valToPos(s.x1, "x", true);
      if (s.source === "special_cause") {
        c.fillStyle = solid;
        c.fillRect(x0, p.bbox.top, x1 - x0, p.bbox.height);
      } else {
        c.strokeStyle = mode === "dark" ? "rgba(204,121,167,0.45)" : "rgba(204,121,167,0.40)";
        c.lineWidth = 1;
        c.beginPath();
        for (let x = x0 - p.bbox.height; x < x1; x += 8) {
          c.moveTo(Math.max(x, x0), p.bbox.top + p.bbox.height - Math.max(0, x0 - x));
          c.lineTo(Math.min(x + p.bbox.height, x1), p.bbox.top + Math.max(0, x + p.bbox.height - x1));
        }
        c.stroke();
      }
    }
    c.fillStyle = mode === "dark" ? "rgba(230,120,40,0.9)" : "rgba(213,94,0,0.85)";
    for (const s of promoted) {
      const x0 = p.valToPos(s.x0, "x", true), x1 = p.valToPos(s.x1, "x", true);
      c.fillRect(x0, p.bbox.top, x1 - x0, Math.max(3, 4 * devicePixelRatio));
    }
    c.restore();
  }

  function hover(p: uPlot) {
    const { left, top: y } = p.cursor;
    const box = wrap;
    if (left == null || y == null || left < 0 || !box) { tip = null; return; }
    const text = windowTip(series, p.posToVal(left, "x"));
    if (!text) { tip = null; return; }
    tip = tipAt(p, box, left, y, text);
  }

  $effect(() => {
    const a = top, b = strip; // b (the discrepancy strip) is drawn first, above L and λ·W
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
    const u2 = new uPlot(
      {
        width, height: RATIO_H,
        series: [
          {},
          { ...HIDDEN_SERIES, label: "ratio lo", paths: stepped },
          { ...HIDDEN_SERIES, label: "ratio hi", paths: stepped },
          { label: "L ÷ λW", stroke, width: 2, paths: stepped, points: { show: false } },
          { label: "1", stroke: grid, width: 1, dash: [4, 3], points: { show: false } },
          { ...HIDDEN_SERIES, label: "envelope lo", paths: stepped },
          { ...HIDDEN_SERIES, label: "envelope hi", paths: stepped },
          { label: "reference", stroke: SYS_COLOR, width: 1.25, dash: [2, 2], paths: stepped, points: { show: false }, show: !!series.systematic },
        ],
        bands: [
          // common-cause envelope (light, beneath), then the measurement interval (dark)
          { ...r.bands[1], fill: mode === "dark" ? "rgba(86,180,233,0.14)" : "rgba(86,180,233,0.12)" },
          { ...r.bands[0], fill: mode === "dark" ? "rgba(170,170,170,0.34)" : "rgba(90,90,90,0.24)" },
        ],
        scales: { y: { distr: 3, log: 2, range: () => ratioRange(r.data) } },
        legend: { show: false },
        axes: plotAxes(stroke, grid, {
          label: "L ÷ λW", size: gutter, splits: () => [0.25, 0.5, 1, 2, 4, 8].filter((v) => { const [a, b] = ratioRange(r.data); return v >= a && v <= b; }),
          values: (_u: uPlot, ts: (number | null)[]) => ts.map((t) => (t == null ? "" : fmtRatio(t))),
        }, { show: false }),
        cursor,
        hooks: { drawClear: [(p: uPlot) => shade(p, mode)], setCursor: [hover] },
      },
      r.data, b,
    );
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
        axes: plotAxes(stroke, grid, { label: "requests in flight", size: gutter }),
        cursor,
        hooks: { drawClear: [(p: uPlot) => shade(p, mode)], setCursor: [hover] },
      },
      m.data, a,
    );
    onRendered(performance.now() - t0, (m.data[0] as number[]).length * 3);
    return () => { u1.destroy(); u2.destroy(); };
  });
</script>

<div class="littles" data-littles-verdict={series.verdict} data-littles-transient={spans.length} data-littles-systematic={series.systematic ? series.systematic.direction : ""} data-littles-promoted={promoted.length}>
  <div class="verdict">
    {seriesTitle(series)}: <b data-littles-discrepancy>{discrepancyText(series)}</b> · {verdictText(series.verdict)}
    <span class="key"><i style="background:{L_COLOR}"></i>L <i class="dash" style="border-color:{LW_COLOR}"></i>λ·W</span>
  </div>
  {#if sysLabel}<div class="sys" data-littles-systematic-label>{sysLabel}</div>{/if}
  <div class="plot" bind:this={wrap} role="presentation" onmouseleave={() => (tip = null)}>
    <div bind:this={strip}></div>
    <div bind:this={top}></div>
    {#if tip}
      <ChartTip {tip} data-littles-tip />
    {/if}
  </div>
  {#if warning}<div class="warn" data-littles-common-cause>{warning}</div>{/if}
  <div class="legend">{littlesLegend(series, data.window_ms)}</div>
</div>

<style>
  .verdict { font-size: 0.85em; margin: 2px 0; display: flex; gap: 12px; align-items: center; }
  .key { opacity: 0.8; display: inline-flex; gap: 4px; align-items: center; }
  .key i { display: inline-block; width: 14px; height: 2px; }
  .key i.dash { height: 0; border-top: 2px dashed; }
  .plot { position: relative; }
  .sys { font-size: 0.8em; color: #CC79A7; font-weight: 600; margin: 0 0 2px; }
  .warn { font-size: 0.78em; opacity: 0.85; margin-top: 2px; }
</style>
