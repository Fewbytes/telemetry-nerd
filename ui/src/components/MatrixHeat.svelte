<script lang="ts">
  import { matrixCellColor, matrixCellTip } from "../chart/matrixHeat";
  import type { SampleHeatmapPanelData } from "../lib/api";
  import CompactText from "./panel/CompactText.svelte";

  let { data, compact = false }: { data: SampleHeatmapPanelData; compact?: boolean } = $props();
  const stops = $derived(
    [0, 0.25, 0.5, 0.75, 1].map((t) => matrixCellColor(data.min + t * (data.max - data.min), data.min, data.max)).join(", "),
  );
</script>

<div class="mh" data-matrix-heat data-matrix-heat-rows={data.row_labels.length} data-matrix-heat-columns={data.column_labels.length}>
  <table aria-label="Matrix heatmap, {data.row_labels.length} rows by {data.column_labels.length} columns">
    <thead>
      <tr><th></th>{#each data.column_labels as c (c)}<th scope="col" class="col"><span>{c}</span></th>{/each}</tr>
    </thead>
    <tbody>
      {#each data.cells as row, r (data.row_labels[r])}
        <tr>
          <th scope="row">{data.row_labels[r]}</th>
          {#each row as v, c (data.column_labels[c])}
            <td style:background={matrixCellColor(v, data.min, data.max)} title={matrixCellTip(data.row_labels[r], data.column_labels[c], v)}></td>
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
  <div class="bar" data-matrix-heat-bar>
    <span>{data.min}</span>
    <span class="ramp" style:background="linear-gradient(to right, {stops})"></span>
    <span>{data.max}</span>
  </div>
  <div class="legend" data-matrix-heat-legend><CompactText text={data.legend} {compact} /></div>
</div>

<style>
  table { border-collapse: collapse; font-size: 0.75em; }
  td { min-width: 14px; height: 14px; padding: 0; }
  th[scope="row"] { text-align: right; padding-right: 6px; font-weight: normal; white-space: nowrap; }
  th.col span { display: inline-block; writing-mode: vertical-rl; font-weight: normal; padding-bottom: 4px; }
  .bar { display: flex; align-items: center; gap: 6px; font-size: 0.75em; margin-top: 4px; }
  .ramp { display: inline-block; width: 160px; height: 8px; border: 1px solid var(--muted, #888); }
  .legend { font-size: 0.8em; margin: 2px 0; }
</style>
