<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { OUTLIER_COLORS, bandFills, outlierText, toFleetUplot, type FleetData } from "../chart/fleet";
  import { decimate, sharedRange } from "../chart/fleetHeat";
  import { HIDDEN_SERIES, plotAxes } from "../chart/plotKit";
  import { fmtTimeZ } from "../lib/format";
  import { plotColors, theme } from "../lib/theme.svelte";

  let { data, width, k = 6, onRendered }: {
    data: FleetData; width: number; k?: number;
    onRendered: (ms: number, points: number, heightPx: number) => void;
  } = $props();

  const GAP = 8, H = 130;
  const shown = $derived(data.outliers.slice(0, k));
  const cols = $derived(width >= 900 ? 3 : width >= 560 ? 2 : 1);
  const cellW = $derived(Math.floor((width - GAP * (cols - 1)) / cols));
  let host = $state<HTMLDivElement | null>(null);

  $effect(() => {
    const root = host;
    if (!root || !shown.length) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(root, mode);
    const fills = bandFills(mode === "dark");
    const range = sharedRange(data, shown.length); // identical y in every panel
    const plots: uPlot[] = [];
    let points = 0;
    shown.forEach((o, i) => {
      const slot = root.children[i].lastElementChild as HTMLElement;
      const one = { ...data, outliers: [o] };
      const m = toFleetUplot(one);
      const maxPts = Math.max(2, 2 * (cellW - 44)); // <= 2 points per px
      const cols_ = decimate(m.data as (number | null)[][], m.roles, maxPts);
      const series: uPlot.Series[] = [{}, ...m.roles.slice(1).map((r): uPlot.Series =>
        r === "median" ? { stroke, width: 1.2, points: { show: false } }
        : r === "outlier" ? { stroke: OUTLIER_COLORS[i % OUTLIER_COLORS.length], width: 1.8, points: { show: false } }
        : HIDDEN_SERIES)];
      plots.push(new uPlot({
        width: cellW, height: H, series,
        bands: m.bands.map((b, j) => ({ ...b, fill: fills[j] })),
        scales: { y: { range: () => range } },
        axes: plotAxes(stroke, grid, { size: 44, font: "10px sans-serif" }, { size: 22, font: "10px sans-serif" }),
        legend: { show: false },
        cursor: { show: false },
      }, cols_ as uPlot.AlignedData, slot));
      points += cols_[0].length * (cols_.length - 1);
    });
    onRendered(performance.now() - t0, points, Math.ceil(shown.length / cols) * (H + 24));
    return () => plots.forEach((p) => p.destroy());
  });
</script>

<div class="sm" bind:this={host} data-fleet-multiples={shown.length} style:grid-template-columns="repeat({cols}, {cellW}px)">
  {#each shown as o, i (o.id)}
    <figure>
      <figcaption><span class="swatch" style:background={OUTLIER_COLORS[i % OUTLIER_COLORS.length]}></span>{outlierText(o, fmtTimeZ)}</figcaption>
      <div></div>
    </figure>
  {/each}
</div>
<div class="legend">same y range in every panel · shading: fleet min–max, 10–90%, 25–75% · grey line: fleet median · coloured line: the member</div>

<style>
  .sm { display: grid; gap: 4px 8px; }
  figure { margin: 0; }
  figcaption { font-size: 0.75em; margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .swatch { display: inline-block; width: 10px; height: 3px; margin-right: 6px; vertical-align: middle; }
  .legend { font-size: 0.8em; margin: 2px 0; }
</style>
