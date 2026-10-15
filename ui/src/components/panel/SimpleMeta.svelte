<script lang="ts">
  // simple mode's short replacement for the "shown" block: what is shown (its aggregation, P4),
  // the band legend, then time range and step. Full provenance, dataset and query are nerd-only.
  import type { PanelData } from "../../lib/api";
  import { fmtInstant, fmtStep } from "../../lib/format";
  import { intervalLegend, shownWhat, shownWhatCompact, type FleetView, type HeatView } from "../../lib/panelNotes";
  import CompactText from "./CompactText.svelte";

  let { data, heatView, fleetView }: {
    data: PanelData;
    heatView: HeatView;
    fleetView: FleetView;
  } = $props();
</script>

<!-- a sentence ending in the bin-scheme list (le buckets) is one line, opened in place; every other
     sentence is the aggregation/estimator basis and stays in full -->
<p class="simple-what" data-simple-what><CompactText text={shownWhat(data, heatView, fleetView)} compact={shownWhatCompact(data, heatView)} /></p>
{#if data.kind === "time" && data.series.some((s) => s.lo)}
  {@const bandLegend = intervalLegend(data.dataset)}
  {#if bandLegend}<div class="legend" data-interval-legend>band: {bandLegend}</div>{/if}
{/if}
<p class="simple-meta" data-simple-meta>
  {fmtInstant(data.dataset.start_ms)} – {fmtInstant(data.dataset.end_ms)} · step {fmtStep(data.effective_step_ms)}
</p>
