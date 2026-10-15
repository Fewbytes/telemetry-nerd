<script lang="ts">
  // simple mode's short replacement for the "shown" block: what is shown (its aggregation, P4),
  // the band legend, then time range and step. Full provenance, dataset and query are nerd-only.
  import type { PanelData } from "../../lib/api";
  import { fmtStep } from "../../lib/format";
  import { intervalLegend, shownWhat } from "../../lib/panelNotes";
  import CompactText from "./CompactText.svelte";

  let { data, heatView, fleetView }: {
    data: PanelData;
    heatView: "heatmap" | "percentiles";
    fleetView: "band" | "quantiles" | "heat" | "multiples";
  } = $props();
  const fmtTime = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");
</script>

<!-- the non-time kinds' sentences carry long scheme lists (le buckets, estimators): one line, opens in place -->
<p class="simple-what" data-simple-what>{#if data.kind === "time"}{shownWhat(data, heatView, fleetView)}{:else}<CompactText text={shownWhat(data, heatView, fleetView)} />{/if}</p>
{#if data.kind === "time" && data.series.some((s) => s.lo)}
  {@const bandLegend = intervalLegend(data.dataset)}
  {#if bandLegend}<div class="legend" data-interval-legend>band: {bandLegend}</div>{/if}
{/if}
<p class="simple-meta" data-simple-meta>
  {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step {fmtStep(data.effective_step_ms)}
</p>
