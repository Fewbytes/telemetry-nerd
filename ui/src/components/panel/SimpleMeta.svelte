<script lang="ts">
  // simple mode's one-line replacement for the "shown" block: time range and step. Full
  // provenance, dataset and query are nerd-only.
  import type { PanelData } from "../../lib/api";
  import { fmtStep } from "../../lib/format";

  let { data }: { data: PanelData } = $props();
  const fmtTime = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");
</script>

<p class="simple-meta" data-simple-meta>
  {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step {fmtStep(data.effective_step_ms)}
</p>
