<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { OUTLIER_COLORS, bandFills, coverageGaps, fleetLegend, outlierText, toFleetUplot } from "../chart/fleet";
  import { HIDDEN_SERIES, plotAxes } from "../chart/plotKit";
  import { fmtTimeZ } from "../lib/format";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { FleetPanelData } from "../lib/api";
  import FleetHeat from "./FleetHeat.svelte";
  import FleetSmallMultiples from "./FleetSmallMultiples.svelte";

  let { data, width, height = 260, onRendered }: {
    data: FleetPanelData; width: number; height?: number;
    onRendered: (ms: number, points: number, heightPx?: number) => void;
  } = $props();

  // band + lines (default), member x time heatmap, or small multiples of the top outliers
  type View = "band" | "heat" | "multiples";
  let view = $state<View>("band");
  const views: [View, string][] = [["band", "band + outliers"], ["heat", "member × time"], ["multiples", "small multiples"]];

  let el = $state<HTMLDivElement | null>(null);

  $effect(() => {
    const host = el;
    if (!host || view !== "band") return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const m = toFleetUplot(data);
    const gaps = coverageGaps(data);
    const dark = mode === "dark";
    const fills = bandFills(dark);
    const labelOf: Record<string, string> = { lo: "min", hi: "max", q10: "10%", q90: "90%", q25: "25%", q75: "75%" };
    let k = 0;
    const series: uPlot.Series[] = [{}, ...m.roles.slice(1).map((r): uPlot.Series => {
      if (r === "median") return { label: "median", stroke, width: 1.5, points: { show: false } };
      if (r === "outlier") {
        const o = data.outliers[k];
        return { label: o.id, stroke: OUTLIER_COLORS[k++ % OUTLIER_COLORS.length], width: 1.5, points: { show: false } };
      }
      return { ...HIDDEN_SERIES, label: labelOf[r] };
    })];
    const u = new uPlot(
      {
        width, height, series,
        bands: m.bands.map((b, i) => ({ ...b, fill: fills[i] })),
        axes: plotAxes(stroke, grid, { label: data.normalise === "member" ? "× own median" : undefined }),
        legend: { show: false },
        cursor: { drag: { x: false, y: false } },
        hooks: {
          draw: [
            (p: uPlot) => {
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              c.save();
              // coverage strip: steps where alive members did not report (darker = more missing)
              const y0 = p.bbox.top + p.bbox.height - 4 * dpr;
              const w = Math.max(1, (p.bbox.width / Math.max(data.ts.length, 1)));
              for (const g of gaps) {
                c.fillStyle = dark ? `rgba(220,220,220,${0.15 + 0.7 * g.share})` : `rgba(60,60,60,${0.15 + 0.7 * g.share})`;
                c.fillRect(p.valToPos(g.x, "x", true) - w / 2, y0, w, 4 * dpr);
              }
              // outlier labels at their last drawn point
              c.font = `${11 * dpr}px sans-serif`;
              data.outliers.forEach((o, i) => {
                let j = o.values.length - 1;
                while (j >= 0 && o.values[j] === null) j--;
                if (j < 0) return;
                c.fillStyle = OUTLIER_COLORS[i % OUTLIER_COLORS.length];
                const x = p.valToPos(data.ts[j] / 1000, "x", true), y = p.valToPos(o.values[j] as number, "y", true);
                c.textAlign = "right";
                c.fillText(o.id, x - 4 * dpr, y - 4 * dpr);
              });
              c.restore();
            },
          ],
        },
      },
      m.data, host,
    );
    onRendered(performance.now() - t0, (m.data[0] as number[]).length * (m.data.length - 1), height);
    return () => u.destroy();
  });
</script>

<div class="fleet" data-fleet-members={data.members} data-fleet-outliers={data.outlier_count} data-fleet-view={view}>
  <div class="legend views" role="group" aria-label="Fleet view">
    view:
    {#each views as [v, label] (v)}
      {#if v === "band" || (v === "heat" && data.heat) || (v === "multiples" && data.outliers.length)}
        <button type="button" class:on={view === v} aria-pressed={view === v} data-fleet-view-btn={v} onclick={() => (view = v)}>{label}</button>
      {/if}
    {/each}
  </div>
  {#if view === "band"}<div bind:this={el}></div>{/if}
  {#if view === "heat"}
    <FleetHeat {data} {width} {onRendered} />
  {:else if view === "multiples"}
    <FleetSmallMultiples {data} {width} {onRendered} />
  {:else}
    <div class="legend">{fleetLegend(data)}</div>
    {#if data.outliers.length}
      <ul class="outliers">
        {#each data.outliers as o, i (o.id)}
          <li><span class="swatch" style:background={OUTLIER_COLORS[i % OUTLIER_COLORS.length]}></span>{outlierText(o, fmtTimeZ)}</li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>

<style>
  .legend { font-size: 0.8em; margin: 2px 0; }
  .views button { font-size: inherit; margin-right: 4px; }
  .views button.on { font-weight: 600; text-decoration: underline; }
  .outliers { font-size: 0.8em; margin: 2px 0 6px 0; padding: 0; list-style: none; }
  .swatch { display: inline-block; width: 10px; height: 3px; margin-right: 6px; vertical-align: middle; }
</style>
