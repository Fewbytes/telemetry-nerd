<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { baselineSpan, spcLegend, toSpcUplot, violationMarks } from "../chart/spc";
  import { seriesName } from "../chart/toUplot";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { SpcPanelData } from "../lib/api";

  let { data, series, width, height = 220, onRendered }: {
    data: SpcPanelData; series: SpcPanelData["series"][number]; width: number; height?: number;
    onRendered: (ms: number, points: number) => void;
  } = $props();

  let el = $state<HTMLDivElement | null>(null);
  const VIOLATION = "#D55E00";
  const flagged = $derived((series.violations ?? []).length);

  $effect(() => {
    const host = el;
    if (!host) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const m = toSpcUplot(series, data.effective_step_ms);
    const xs = m.data[0] as number[];
    const marks = violationMarks(series);
    const u = new uPlot(
      {
        width, height,
        series: [
          {},
          { label: seriesName(series.labels), stroke: "#0072B2", width: 1.25 },
          { label: "centre", stroke, width: 1, dash: [6, 3], points: { show: false } },
          { label: "−3σ", stroke: grid, width: 1, points: { show: false } },
          { label: "+3σ", stroke: grid, width: 1, points: { show: false } },
        ],
        bands: m.bands.map((b) => ({ ...b, fill: mode === "dark" ? "rgba(140,140,140,0.18)" : "rgba(120,120,120,0.12)" })),
        axes: [
          { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
          { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
        ],
        cursor: { drag: { x: false, y: false } },
        hooks: {
          drawClear: [
            (p: uPlot) => {
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
            (p: uPlot) => {
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              c.save();
              c.lineWidth = 1.5 * dpr;
              c.strokeStyle = VIOLATION; c.fillStyle = VIOLATION;
              for (const mk of marks) {
                const x = p.valToPos(mk.x, "x", true), y = p.valToPos(mk.y, "y", true);
                c.beginPath(); c.arc(x, y, 3.5 * dpr, 0, 2 * Math.PI);
                if (mk.deciding) c.fill(); else c.stroke();
              }
              c.restore();
            },
          ],
        },
      },
      m.data, host,
    );
    onRendered(performance.now() - t0, xs.length);
    return () => u.destroy();
  });
</script>

<div class="spc" data-spc-violations={flagged} data-spc-mode={series.mode}>
  <div class="verdict">{seriesName(series.labels)}: <b>{series.verdict.replace("_", " ")}</b>{series.also.length ? ` (also ${series.also.join(", ").replaceAll("_", " ")})` : ""}</div>
  <div bind:this={el}></div>
  <div class="legend">{spcLegend(series, data.baseline)}</div>
</div>

<style>
  .verdict { font-size: 0.85em; margin: 2px 0; }
</style>
