<script lang="ts">
  // what the panel shows (dataset, step, provenance, time range) and the query behind it
  import type { PanelData } from "../../lib/api";
  import { provenanceParts, provenanceText, shownWhat } from "../../lib/panelNotes";
  import { fmtInstant, fmtStep } from "../../lib/format";

  let { data, heatView, fleetView, openCode }: {
    data: PanelData;
    heatView: "heatmap" | "percentiles";
    fleetView: "band" | "quantiles" | "heat" | "multiples";
    openCode: ((id: string) => void) | undefined;
  } = $props();

  const parts = $derived(provenanceParts(data.dataset));
</script>

<div class="shown">
  <p class="what">{shownWhat(data, heatView, fleetView)}</p>
  <p class="where">
    <span data-provenance>{#if parts}{@const [pre, node, post] = parts}{pre}<button type="button" class="ref-chip obj-id" data-code-link={node} title="View the code of {node}" onclick={() => openCode?.(node)}>{node}</button>{post}{:else}{provenanceText(data.dataset)}{/if}</span> · {fmtInstant(data.dataset.start_ms)} – {fmtInstant(data.dataset.end_ms)} · step
        {fmtStep(data.effective_step_ms)}
  </p>
</div>
<details class="query">
  <summary>Query</summary>
  <pre><code>{data.dataset.expr}</code></pre>
</details>
