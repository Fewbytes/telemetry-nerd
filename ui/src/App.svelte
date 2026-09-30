<script lang="ts">
  import { fetchPanels, subscribe, type Panel } from "./api";
  import PanelView from "./Panel.svelte";

  let panels = $state<Panel[]>([]);
  let error = $state<string | null>(null);

  $effect(() => {
    const load = () =>
      fetchPanels()
        .then((p) => (panels = p))
        .catch((e) => (error = String(e)));
    load();
    return subscribe((e) => {
      if (e.type === "panel.created") load();
    });
  });

  $effect(() => {
    const match = location.hash.match(/^#\/panel\/(\w+)$/);
    if (match && panels.length > 0) {
      document.getElementById(`panel-${match[1]}`)?.scrollIntoView();
    }
  });
</script>

<main>
  <h1>Telemetry Nerd</h1>
  {#if error}<div class="error">{error}</div>{/if}
  {#if panels.length === 0}
    <p class="empty">No panels yet. Ask Claude a question about your metrics.</p>
  {/if}
  {#each panels as panel (panel.id)}
    <PanelView {panel} />
  {/each}
</main>
