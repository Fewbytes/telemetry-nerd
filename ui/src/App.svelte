<script lang="ts">
  import { createWorkspace } from "./lib/workspace.svelte";
  import PanelView from "./Panel.svelte";

  const ws = createWorkspace();
  $effect(() => ws.start());

  const panels = $derived((ws.snapshot?.panels ?? []).filter((p) => !p.closed));

  $effect(() => {
    const match = location.hash.match(/^#\/panel\/(\w+)$/);
    if (match && panels.length > 0) {
      document.getElementById(`panel-${match[1]}`)?.scrollIntoView();
    }
  });
</script>

<main>
  <h1>Telemetry Nerd</h1>
  {#if ws.error}<div class="error">{ws.error}</div>{/if}
  {#if panels.length === 0}
    <p class="empty">No panels yet. Ask Claude a question about your metrics.</p>
  {/if}
  {#each panels as panel (panel.id)}
    <PanelView {panel} annotations={ws.snapshot?.annotations ?? []} />
  {/each}
</main>
