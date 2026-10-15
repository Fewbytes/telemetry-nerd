<script lang="ts">
  // the picked range drawn as a separate preview chart (Panel draws into previewEl); explicitly
  // not the panel's evidence. Shared by the nerd controls and the simple toolbar.
  import type { Panel } from "../../lib/api";
  import type { PreviewRender } from "../../chart/preview";

  let { panel, previewRender, previewEl = $bindable() }: {
    panel: Panel;
    previewRender: PreviewRender | null;
    previewEl: HTMLDivElement | null;
  } = $props();
</script>

{#if previewRender}
  <div class="preview-render" data-preview-render={previewRender.source}>
    <span class="preview-tag">preview — not {panel.id}'s evidence</span>
    <div bind:this={previewEl} class="preview-plot"></div>
  </div>
{/if}
