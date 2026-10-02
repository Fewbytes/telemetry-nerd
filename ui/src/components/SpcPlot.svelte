<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { baselineSpan, DECIDING, markTip, nearestMark, spcLegend, toSpcUplot, violationMarks, type SpcMark } from "../chart/spc";
  import { drawDots, HIDDEN_SERIES, plotAxes, tipAt, type HoverTip } from "../chart/plotKit";
  import { seriesName } from "../chart/toUplot";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { SpcPanelData } from "../lib/api";
  import ChartTip from "./ChartTip.svelte";

  let { data, series, width, height = 220, onRendered }: {
    data: SpcPanelData; series: SpcPanelData["series"][number]; width: number; height?: number;
    onRendered: (ms: number, points: number) => void;
  } = $props();

  let el = $state<HTMLDivElement | null>(null);
  let wrap = $state<HTMLDivElement | null>(null);
  /** supplementary run rules (2 of 3, 4 of 5, 8 in a row): drawn hollow, can be hidden */
  let supplementary = $state(true);
  let tip = $state<HoverTip | null>(null);
  const VIOLATION = "#D55E00";
  const marks = $derived(violationMarks(series, supplementary));
  const hasSupplementary = $derived((series.violations ?? []).some((v) => !v.rules.some((r) => DECIDING.has(r))));
  // read by the draw/cursor hooks, so toggling only redraws (the plot is not re-created)
  let drawn: SpcMark[] = [];
  let plot: uPlot | null = null;

  $effect(() => {
    drawn = marks;
    tip = null;
    plot?.redraw(false);
  });

  $effect(() => {
    const host = el;
    if (!host) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const m = toSpcUplot(series, data.effective_step_ms);
    const xs = m.data[0] as number[];
    const step = data.effective_step_ms;
    const fills = [
      mode === "dark" ? "rgba(140,140,140,0.18)" : "rgba(120,120,120,0.12)",
      ...Array(3).fill(mode === "dark" ? "rgba(170,170,170,0.30)" : "rgba(80,80,80,0.22)"),
    ];
    const u = new uPlot(
      {
        width, height,
        series: [
          {},
          { label: seriesName(series.labels), stroke: "#0072B2", width: 1.25 },
          { label: "centre", stroke, width: 1, dash: [6, 3], points: { show: false } },
          { label: "−3σ", stroke: grid, width: 1, points: { show: false } },
          { label: "+3σ", stroke: grid, width: 1, points: { show: false } },
          ...["+3σ 99% lo", "+3σ 99% hi", "−3σ 99% lo", "−3σ 99% hi", "centre 99% lo", "centre 99% hi"].map((label) => ({ ...HIDDEN_SERIES, label })),
        ],
        bands: m.bands.map((b, k) => ({ ...b, fill: fills[k] })),
        legend: { show: false },
        axes: plotAxes(stroke, grid),
        cursor: { drag: { x: false, y: false }, points: { show: false } },
        hooks: {
          drawClear: [
            (p: uPlot) => {
              if (data.baseline.kind === "reference") return; // a separate earlier window: not plotted
              const span = xs.length ? baselineSpan(data.baseline, xs[0], xs[xs.length - 1]) : null;
              if (!span) return;
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              const x0 = p.valToPos(span[0], "x", true), x1 = p.valToPos(span[1], "x", true);
              c.save();
              c.fillStyle = mode === "dark" ? "rgba(86,180,233,0.10)" : "rgba(86,180,233,0.12)";
              c.fillRect(x0, p.bbox.top, x1 - x0, p.bbox.height);
              c.fillStyle = stroke; c.font = `${10 * dpr}px sans-serif`;
              c.fillText("baseline", x0 + 4 * dpr, p.bbox.top + 12 * dpr);
              c.restore();
            },
          ],
          draw: [
            (p: uPlot) => drawDots(p, drawn, VIOLATION, (mk) => mk.deciding),
          ],
          setCursor: [
            (p: uPlot) => {
              const { left, top } = p.cursor;
              const box = wrap;
              if (left == null || top == null || left < 0 || !box) { tip = null; return; }
              const hit = nearestMark(drawn, left, top, (mk) => [p.valToPos(mk.x, "x"), p.valToPos(mk.y, "y")]);
              if (!hit) { tip = null; return; }
              tip = tipAt(p, box, left, top, markTip(hit, step));
            },
          ],
        },
      },
      m.data, host,
    );
    plot = u;
    onRendered(performance.now() - t0, xs.length);
    return () => { plot = null; u.destroy(); };
  });
</script>

<div class="spc" data-spc-violations={marks.length} data-spc-mode={series.mode} data-spc-seasonal={series.seasonal ?? "none"}>
  <div class="verdict">{seriesName(series.labels)}: <b>{series.verdict.replace("_", " ")}</b>{series.also.length ? ` (also ${series.also.join(", ").replaceAll("_", " ")})` : ""}</div>
  <div class="plot" bind:this={wrap} role="presentation" onmouseleave={() => (tip = null)}>
    <div bind:this={el}></div>
    {#if tip}
      <ChartTip {tip} data-spc-tip />
    {/if}
  </div>
  <div class="legend">
    {spcLegend(series, data.baseline, supplementary)}
    {#if hasSupplementary}
      <label class="supp"><input type="checkbox" bind:checked={supplementary} data-spc-supplementary /> supplementary run rules</label>
    {/if}
  </div>
</div>

<style>
  .verdict { font-size: 0.85em; margin: 2px 0; }
  .plot { position: relative; }
  .supp { margin-left: 6px; white-space: nowrap; }
</style>
