<script lang="ts">
  import { layoutBars } from "../chart/bar";
  import { PALETTE_DARK, PALETTE_LIGHT } from "../chart/toUplot";
  import { theme } from "../lib/theme.svelte";
  import type { BarPanelData } from "../lib/api";

  let { data, width, unit = null }: { data: BarPanelData; width: number; unit?: string | null } = $props();

  const layout = $derived(layoutBars(data.rows, data.order));
  const fill = $derived((theme.effective === "light" ? PALETTE_LIGHT : PALETTE_DARK)[0]);
  let asTable = $state(false);

  const THICKNESS = 22; // px per bar (horizontal) and the chart height when vertical
  const LABEL_GUTTER = 120;
  const vertical = $derived(layout.orientation === "vertical");
  const plotHeight = $derived(vertical ? 180 : layout.bars.length * THICKNESS);
  const plotWidth = $derived(vertical ? width : Math.max(80, width - LABEL_GUTTER));
  const along = $derived(vertical ? plotHeight : plotWidth);
  const slot = $derived(vertical ? width / layout.bars.length : THICKNESS);
  // value axis position (px) of a domain fraction: vertical grows upward, horizontal rightward
  const at = (f: number) => (vertical ? along * (1 - f) : along * f);
</script>

<div class="bar-plot" data-bar-plot data-orientation={layout.orientation}>
  <button type="button" class="view-toggle" data-bar-table-toggle aria-pressed={asTable} onclick={() => (asTable = !asTable)}>
    {asTable ? "view as chart" : "view as table"}
  </button>
  {#if asTable}
    <table data-bar-table>
      <thead><tr><th>label</th><th>value{unit ? ` (${unit})` : ""}</th></tr></thead>
      <tbody>
        {#each layout.table as row (row.label)}<tr><td>{row.label}</td><td>{row.value}</td></tr>{/each}
      </tbody>
    </table>
  {:else}
    <svg width={width} height={plotHeight + (vertical ? 20 : 0)} role="img" aria-label={`bar chart, ${layout.bars.length} bars`}>
      <g transform={vertical ? "" : `translate(${LABEL_GUTTER} 0)`}>
        {#each layout.bars as bar, i (bar.label)}
          {#if bar.span}
            {@const a = at(bar.span[0])}
            {@const b = at(bar.span[1])}
            <rect data-bar={bar.label} fill={fill}
              x={vertical ? i * slot + slot * 0.1 : Math.min(a, b)} y={vertical ? Math.min(a, b) : i * slot + 2}
              width={vertical ? slot * 0.8 : Math.abs(b - a)} height={vertical ? Math.abs(b - a) : slot - 4} />
            {#if layout.valueLabels}
              <text class="value" x={vertical ? i * slot + slot / 2 : Math.max(a, b) + 4} y={vertical ? Math.min(a, b) - 3 : i * slot + slot / 2 + 4}
                text-anchor={vertical ? "middle" : "start"}>{bar.value}</text>
            {/if}
          {:else}
            <text class="value" data-bar-missing={bar.label} x={vertical ? i * slot + slot / 2 : at(layout.baseline) + 4}
              y={vertical ? at(layout.baseline) - 3 : i * slot + slot / 2 + 4} text-anchor={vertical ? "middle" : "start"}>no value</text>
          {/if}
          <text class="label" x={vertical ? i * slot + slot / 2 : -6} y={vertical ? plotHeight + 14 : i * slot + slot / 2 + 4}
            text-anchor={vertical ? "middle" : "end"}>{bar.label}</text>
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
  .value { fill: currentColor; }
  .label { fill: currentColor; }
  table { border-collapse: collapse; }
  td, th { padding: 2px 10px; text-align: left; }
</style>
