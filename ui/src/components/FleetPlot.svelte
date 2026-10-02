<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { OUTLIER_COLORS, coverageGaps, fleetLegend, outlierText, toFleetUplot } from "../chart/fleet";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { FleetPanelData } from "../lib/api";

  let { data, width, height = 260, onRendered }: {
    data: FleetPanelData; width: number; height?: number;
    onRendered: (ms: number, points: number) => void;
  } = $props();

  let el = $state<HTMLDivElement | null>(null);
  const fmtTime = (ms: number) => new Date(ms).toISOString().slice(11, 16) + "Z";

  $effect(() => {
    const host = el;
    if (!host) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const m = toFleetUplot(data);
    const gaps = coverageGaps(data);
    const dark = mode === "dark";
    const fills = dark
      ? ["rgba(150,150,150,0.14)", "rgba(150,150,150,0.20)", "rgba(150,150,150,0.30)"]
      : ["rgba(110,110,110,0.10)", "rgba(110,110,110,0.16)", "rgba(110,110,110,0.26)"];
    const edge: uPlot.Series = { stroke: "transparent", width: 0, points: { show: false } };
    const labelOf: Record<string, string> = { lo: "min", hi: "max", q10: "10%", q90: "90%", q25: "25%", q75: "75%" };
    let k = 0;
    const series: uPlot.Series[] = [{}, ...m.roles.slice(1).map((r): uPlot.Series => {
      if (r === "median") return { label: "median", stroke, width: 1.5, points: { show: false } };
      if (r === "outlier") {
        const o = data.outliers[k];
        return { label: o.id, stroke: OUTLIER_COLORS[k++ % OUTLIER_COLORS.length], width: 1.5, points: { show: false } };
      }
      return { ...edge, label: labelOf[r] };
    })];
    const u = new uPlot(
      {
        width, height, series,
        bands: m.bands.map((b, i) => ({ ...b, fill: fills[i] })),
        axes: [
          { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
          { stroke, grid: { stroke: grid }, ticks: { stroke: grid }, label: data.normalise === "member" ? "× own median" : undefined },
        ],
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
    onRendered(performance.now() - t0, (m.data[0] as number[]).length * (m.data.length - 1));
    return () => u.destroy();
  });
</script>

<div class="fleet" data-fleet-members={data.members} data-fleet-outliers={data.outlier_count}>
  <div bind:this={el}></div>
  <div class="legend">{fleetLegend(data)}</div>
  {#if data.outliers.length}
    <ul class="outliers">
      {#each data.outliers as o, i (o.id)}
        <li><span class="swatch" style:background={OUTLIER_COLORS[i % OUTLIER_COLORS.length]}></span>{outlierText(o, fmtTime)}</li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .legend { font-size: 0.8em; margin: 2px 0; }
  .outliers { font-size: 0.8em; margin: 2px 0 6px 0; padding: 0; list-style: none; }
  .swatch { display: inline-block; width: 10px; height: 3px; margin-right: 6px; vertical-align: middle; }
</style>
