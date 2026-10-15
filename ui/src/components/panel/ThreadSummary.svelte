<script lang="ts">
  // simple mode: an anchored thread is one summary line; click opens the full thread in place
  import type { Thread } from "../../lib/api";
  import { threadSummary } from "../../lib/threadSummary";
  import PanelThread from "../PanelThread.svelte";

  let { thread }: { thread: Thread } = $props();
  let open = $state(false);
</script>

<div class="thread-summary">
  <button type="button" class="thread-toggle" aria-expanded={open} onclick={() => (open = !open)}>
    <span aria-hidden="true">💬</span> <span class="thread-line">{threadSummary(thread)}</span>
  </button>
  {#if open}<PanelThread {thread} />{/if}
</div>
