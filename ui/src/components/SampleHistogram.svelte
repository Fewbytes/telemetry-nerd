<script lang="ts">
  import { layoutSampleBars, sampleBarTip } from "../chart/sampleHistogram";
  import { PALETTE_DARK, PALETTE_LIGHT } from "../chart/toUplot";
  import type { SampleHistogramPanelData } from "../lib/api";
  import { theme } from "../lib/theme.svelte";
  import CompactText from "./panel/CompactText.svelte";

  let { data, width, compact = false }: { data: SampleHistogramPanelData; width: number; compact?: boolean } = $props();
  const HEIGHT = 140;
  const palette = $derived(theme.effective === "light" ? PALETTE_LIGHT : PALETTE_DARK);
  const yMax = $derived(Math.max(0, ...data.series.flatMap((s) => s.counts)));
  const first = $derived(data.edges[0]);
  const last = $derived(data.edges[data.edges.length - 1]);
</script>

<div class="sh" data-sample-histogram>
  {#each data.series as s, i (s.label)}
    <div class="legend">{s.label}</div>
    <svg {width} height={HEIGHT + 16} role="img" aria-label={`histogram of ${s.label}, n=${s.n}`}>
      {#each layoutSampleBars(data.edges, s.counts, yMax, width, HEIGHT) as b (b.x)}
        <rect data-sample-bar x={b.x} y={b.y} width={b.width} height={b.height} fill={palette[i % palette.length]}><title>{sampleBarTip(b)}</title></rect>
      {/each}
      <text x="0" y={HEIGHT + 12} class="edge">{first}</text>
      <text x={width} y={HEIGHT + 12} class="edge" text-anchor="end">{last}</text>
    </svg>
  {/each}
  <div class="legend" data-sample-histogram-legend><CompactText text={data.text} {compact} /></div>
</div>

<style>
  .legend { font-size: 0.8em; margin: 2px 0; }
  .edge { font-size: 0.75em; fill: currentColor; }
</style>
