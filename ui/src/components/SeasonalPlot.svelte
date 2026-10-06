<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { flagMarks, seasonalLegend, seriesDisplayName, toSeasonalUplot, verdictText, type SeasonalView } from "../chart/seasonal";
  import { drawDots, plotAxes, axisGutterSize } from "../chart/plotKit";
  import { fmtRatio } from "../chart/indexed";
  import { fmtSI } from "../lib/format";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { SeasonalPanelData } from "../lib/api";

  let { data, series, width, height = 220, onRendered, onPlot }: {
    data: SeasonalPanelData; series: SeasonalPanelData["series"][number]; width: number; height?: number;
    onRendered: (ms: number, points: number) => void;
    onPlot?: (u: uPlot) => void;
  } = $props();

  let el = $state<HTMLDivElement | null>(null);
  let view = $state<SeasonalView>("overlay");
  const NOW = "#0072B2", FLAG = "#D55E00";
  const canRatio = $derived(!!series.ratio);

  $effect(() => {
    const host = el;
    if (!host) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const v = canRatio ? view : "overlay";
    const m = toSeasonalUplot(series, v);
    const marks = flagMarks(series, v);
    const faint = mode === "dark" ? "rgba(200,200,200,0.28)" : "rgba(90,90,90,0.25)";
    const style: Record<string, uPlot.Series> = {
      cycle: { stroke: faint, width: 1, points: { show: false } },
      lo: { label: "band 5%", stroke: grid, width: 1, points: { show: false } },
      hi: { label: "band 95%", stroke: grid, width: 1, points: { show: false } },
      centre: { label: "median of cycles", stroke, width: 1, dash: [6, 3], points: { show: false } },
      neutral: { label: series.ratio?.kind === "difference" ? "0" : "1", stroke, width: 1, dash: [6, 3], points: { show: false } },
      now: { label: seriesDisplayName(series, data.dataset.expr), stroke: NOW, width: 1.75 },
    };
    const logY = v === "ratio" && series.ratio?.kind === "ratio";
    const unit = data.panel.spec.y.unit;
    const u = new uPlot(
      {
        width, height,
        series: [{}, ...m.roles.slice(1).map((r, i) => (r === "cycle" ? { ...style.cycle, label: `−${series.cycles[i].j}` } : style[r]))],
        bands: m.bands.map((b) => ({ ...b, fill: mode === "dark" ? "rgba(140,140,140,0.20)" : "rgba(120,120,120,0.14)" })),
        scales: logY ? { y: { distr: 3, log: 2 } } : {},
        axes: plotAxes(stroke, grid, {
          label: v === "ratio" ? (logY ? "now ÷ reference (log)" : "now − reference") : unit ?? undefined,
          size: axisGutterSize(),
          values: logY
            ? (_u: uPlot, ts: (number | null)[]) => ts.map((t) => (t == null ? "" : fmtRatio(t)))
            : (_u: uPlot, ts: (number | null)[]) => ts.map((t) => (t == null ? "" : fmtSI(t, unit))),
        }),
        legend: { show: false },
        cursor: { drag: { setScale: false, x: true, y: false } },
        hooks: {
          draw: [(p: uPlot) => drawDots(p, marks, FLAG), (p: uPlot) => onPlot?.(p)],
        },
      },
      m.data, host,
    );
    onRendered(performance.now() - t0, (m.data[0] as number[]).length * (m.data.length - 1));
    return () => u.destroy();
  });
</script>

<div class="seasonal" data-seasonal-verdict={series.verdict} data-seasonal-view={view} data-seasonal-flags={(series.flagged ?? []).length}>
  <div class="head">
    <span>{seriesDisplayName(series, data.dataset.expr)}: <b>{verdictText(series)}</b></span>
    {#if canRatio}
      <span class="views" role="group" aria-label="view">
        <button class:on={view === "overlay"} onclick={() => (view = "overlay")}>cycles</button>
        <button class:on={view === "ratio"} onclick={() => (view = "ratio")}>{series.ratio?.kind === "difference" ? "difference" : "ratio"}</button>
      </span>
    {/if}
  </div>
  <div bind:this={el}></div>
  <div class="legend">{seasonalLegend(series, data.tz, view)}</div>
  {#if series.reasons.length && series.verdict !== "insufficient_history"}
    <ul class="reasons">{#each series.reasons as r, i (i)}<li>{r}</li>{/each}</ul>
  {/if}
</div>

<style>
  .head { display: flex; justify-content: space-between; align-items: center; font-size: 0.85em; margin: 2px 0; }
  .views button { font-size: 0.85em; padding: 1px 6px; border: 1px solid var(--border, #999); background: transparent; color: inherit; cursor: pointer; }
  .views button.on { background: var(--accent-soft, rgba(0, 114, 178, 0.15)); }
  .reasons { font-size: 0.8em; margin: 2px 0 6px 1em; padding: 0; }
</style>
