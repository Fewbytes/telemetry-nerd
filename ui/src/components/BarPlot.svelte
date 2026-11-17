<script lang="ts">
  import { BAR_VALUE_ROOM_PX, barLabelGutterPx, layoutBarGroups } from "../chart/bar";
  import { PALETTE_DARK, PALETTE_LIGHT } from "../chart/toUplot";
  import { theme } from "../lib/theme.svelte";
  import type { BarOrder, BarSeries } from "../chart/bar";

  let { series, order, width, unit = null }: { series: BarSeries[]; order: BarOrder; width: number; unit?: string | null } = $props();

  const layout = $derived(layoutBarGroups(series, order));
  const palette = $derived(theme.effective === "light" ? PALETTE_LIGHT : PALETTE_DARK);
  const grouped = $derived(series.length > 1);
  let asTable = $state(false);

  const THICKNESS = 22; // px per bar (horizontal) and the chart height when vertical
  const labelGutter = $derived(barLabelGutterPx(layout.categories));
  const vertical = $derived(layout.orientation === "vertical");
  const plotHeight = $derived(vertical ? 180 : layout.categories.length * THICKNESS * series.length);
  const plotWidth = $derived(vertical ? width : Math.max(80, width - labelGutter - BAR_VALUE_ROOM_PX));
  const along = $derived(vertical ? plotHeight : plotWidth);
  // a category's slot holds its series' bars side by side
  const slot = $derived(vertical ? width / layout.categories.length : THICKNESS * series.length);
  const barSlot = $derived(slot / series.length);
  // value axis position (px) of a domain fraction: vertical grows upward, horizontal rightward
  const at = (f: number) => (vertical ? along * (1 - f) : along * f);
</script>

<div class="bar-plot" data-bar-plot data-orientation={layout.orientation}>
  <button type="button" class="view-toggle" data-bar-table-toggle aria-pressed={asTable} onclick={() => (asTable = !asTable)}>
    {asTable ? "view as chart" : "view as table"}
  </button>
  {#if grouped}
    <div class="legend">
      {#each layout.seriesLabels as name, j (name)}<span data-bar-series={name}><i style:background={palette[j]}></i>{name}</span>{/each}
    </div>
  {/if}
  {#if asTable}
    <table data-bar-table>
      <thead><tr><th>label</th>{#each layout.seriesLabels as name (name)}<th>{name}{unit ? ` (${unit})` : ""}</th>{/each}</tr></thead>
      <tbody>
        {#each layout.table as row (row.label)}<tr><td>{row.label}</td>{#each row.values as v, j (j)}<td>{v}</td>{/each}</tr>{/each}
      </tbody>
    </table>
  {:else}
    <svg width={width} height={plotHeight + (vertical ? BAR_VALUE_ROOM_PX / 2 + 20 : 0)} role="img" aria-label={`bar chart, ${layout.categories.length} categories, ${series.length} series`}>
      <g transform={vertical ? `translate(0 ${BAR_VALUE_ROOM_PX / 2})` : `translate(${labelGutter} 0)`}>
        {#each layout.categories as cat, i (cat.label)}
          {#each cat.bars as bar, j (j)}
            {@const pos = i * slot + j * barSlot}
            {#if bar.span}
              {@const a = at(bar.span[0])}
              {@const b = at(bar.span[1])}
              <rect data-bar={cat.label} data-bar-series={layout.seriesLabels[j]} fill={palette[j]}
                x={vertical ? pos + barSlot * 0.1 : Math.min(a, b)} y={vertical ? Math.min(a, b) : pos + 2}
                width={vertical ? barSlot * 0.8 : Math.abs(b - a)} height={vertical ? Math.abs(b - a) : barSlot - 4} />
              {#if layout.valueLabels}
                <text class="value" x={vertical ? pos + barSlot / 2 : Math.max(a, b) + 4} y={vertical ? Math.min(a, b) - 3 : pos + barSlot / 2 + 4}
                  text-anchor={vertical ? "middle" : "start"}>{bar.value}</text>
              {/if}
            {:else}
              <text class="value" data-bar-missing={cat.label} x={vertical ? pos + barSlot / 2 : at(layout.baseline) + 4}
                y={vertical ? at(layout.baseline) - 3 : pos + barSlot / 2 + 4} text-anchor={vertical ? "middle" : "start"}>no value</text>
            {/if}
          {/each}
          <text class="label" x={vertical ? i * slot + slot / 2 : -6} y={vertical ? plotHeight + 14 : i * slot + slot / 2 + 4}
            text-anchor={vertical ? "middle" : "end"}>{cat.label}</text>
        {/each}
        <line data-bar-baseline stroke="currentColor" opacity="0.5"
          x1={vertical ? 0 : at(layout.baseline)} x2={vertical ? width : at(layout.baseline)}
          y1={vertical ? at(layout.baseline) : 0} y2={vertical ? at(layout.baseline) : plotHeight} />
      </g>
    </svg>
  {/if}
</div>

<style>
  .bar-plot { font-size: 12px; color: var(--muted, #666); }
  .view-toggle { font-size: 12px; margin-bottom: 4px; }
  .legend { display: flex; gap: 12px; margin-bottom: 4px; }
  .legend i { display: inline-block; width: 10px; height: 10px; margin-right: 4px; }
  .value { fill: currentColor; }
  .label { fill: currentColor; }
  table { border-collapse: collapse; }
  td, th { padding: 2px 10px; text-align: left; }
</style>
