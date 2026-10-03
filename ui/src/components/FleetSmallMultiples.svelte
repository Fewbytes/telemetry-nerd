<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { FLEET_HUE, MUTED_LINE, OUTLIER_COLORS, THRESHOLD_DASH, bandFills, bandView, groupFill, groupStyle, groupZoneFills, grouped, outlierText, spcOf, toFleetUplot, type FleetData } from "../chart/fleet";
  import { decimate, sharedRange } from "../chart/fleetHeat";
  import { HIDDEN_SERIES, plotAxes } from "../chart/plotKit";
  import { fmtTimeZ } from "../lib/format";
  import { plotColors, theme } from "../lib/theme.svelte";

  let { data, width, k = 6, range: given = null, onRendered }: {
    data: FleetData; width: number; k?: number; range?: [number, number] | null;
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
    const view = bandView(data, "spc");
    const dark = mode === "dark";
    // grouped: two zones per group (SPC) or the whole min-max then one IQR per group (quantiles)
    const fills = !grouped(data) ? bandFills(dark, view)
      : view === "spc" ? (data.clusters ?? []).flatMap((_, g) => groupZoneFills(dark, g))
      : [bandFills(dark)[0], ...(data.clusters ?? []).map((_, g) => groupFill(dark, g))];
    const dpr = window.devicePixelRatio || 1;
    const muted = MUTED_LINE[mode === "dark" ? "dark" : "light"];
    const range = given ?? sharedRange(data, shown.length); // identical y in every panel (the bounded range when known)
    const plots: uPlot[] = [];
    let points = 0;
    shown.forEach((o, i) => {
      const slot = root.children[i].lastElementChild as HTMLElement;
      const one = { ...data, outliers: [o] };
      const m = toFleetUplot(one, view);
      let g = 0; // group medians, in order
      const maxPts = Math.max(2, 2 * (cellW - 44)); // <= 2 points per px
      const cols_ = decimate(m.data as (number | null)[][], m.roles, maxPts);
      const series: uPlot.Series[] = [{}, ...m.roles.slice(1).map((r): uPlot.Series =>
        r === "median" ? { stroke: FLEET_HUE[mode === "dark" ? "dark" : "light"].line, width: 1.5, points: { show: false } }
        : r === "cmedian" ? { stroke: groupStyle(dark, g).line, width: 1.5, dash: [...groupStyle(dark, g++).dash], points: { show: false } }
        : r === "outlier" || r === "episode" ? { stroke: OUTLIER_COLORS[i % OUTLIER_COLORS.length], width: 1.8, points: { show: false } }
        : r === "muted" ? { stroke: muted, width: 1.2, points: { show: false } }
        : r === "thrlo" || r === "thrhi" ? { stroke: muted, width: 1, dash: THRESHOLD_DASH.map((v) => v * dpr), points: { show: false } }
        : HIDDEN_SERIES)];
      plots.push(new uPlot({
        width: cellW, height: H, series,
        tzDate: (ts: number) => uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"),
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
<div class="legend">same y range in every panel · shading: {bandView(data, "spc") === "spc" ? `${grouped(data) ? "each group's" : "the fleet's"} ${(spcOf(data, data.outliers[0]) ?? data.clusters?.[0]?.spc)?.legend ?? "SPC band"}; dashed: flag bar (approximate)` : "fleet min–max, 10–90%, 25–75%"} · coloured line: the member (a transient: grey, its episodes coloured)</div>

<style>
  .sm { display: grid; gap: 4px 8px; }
  figure { margin: 0; }
  figcaption { font-size: 0.75em; margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .swatch { display: inline-block; width: 10px; height: 3px; margin-right: 6px; vertical-align: middle; }
  .legend { font-size: 0.8em; margin: 2px 0; }
</style>
