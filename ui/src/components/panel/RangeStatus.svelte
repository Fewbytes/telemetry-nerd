<script lang="ts">
  // the picked range's status (preview badge + "keep this range", or the range error); shared by
  // the nerd time-range row and the simple toolbar. No wrapper: it sits inside the caller's row.
  import type { Viewport } from "../../chart/viewport";
  import type { PreviewRender } from "../../chart/preview";
  import { fmtRange } from "../../lib/format";

  let { viewport, previewRender, rangeBusy, rangeError, keepThisRange }: {
    viewport: Viewport | null;
    previewRender: PreviewRender | null;
    rangeBusy: boolean;
    rangeError: string | null;
    keepThisRange: () => void;
  } = $props();
</script>

{#if viewport && !rangeBusy && !rangeError}
  <span class="hint" data-preview-badge>
    {#if previewRender}
      previewing {fmtRange(previewRender.start_ms, previewRender.end_ms)}
            ({previewRender.source === "local" ? "from data already loaded" : "fetched"}) — not this panel's evidence
    {:else}
      selected {fmtRange(viewport.start_ms, viewport.end_ms)} — summary only, no chart for this kind of data
    {/if}
  </span>
  <button type="button" disabled={rangeBusy} onclick={keepThisRange}>keep this range</button>
{/if}
{#if rangeError}<span class="hint" data-range-error>{rangeError}</span>{/if}
