<script lang="ts">
  import { createWorkspace } from "./lib/workspace.svelte";
  import PanelView from "./Panel.svelte";
  import Sidebar from "./components/Sidebar.svelte";

  const ws = createWorkspace();
  $effect(() => ws.start());

  const panels = $derived((ws.snapshot?.panels ?? []).filter((p) => !p.closed));
  const threads = $derived(ws.snapshot?.threads ?? []); // anchored ones render inside Panel

  $effect(() => {
    const scrollToHash = () => {
      const match = location.hash.match(/^#\/panel\/(\w+)$/);
      if (match && panels.length > 0) {
        document.getElementById(`panel-${match[1]}`)?.scrollIntoView();
      }
    };
    scrollToHash();
    // evidence-ref links (#/panel/pN) only change the hash; listen for that too
    window.addEventListener("hashchange", scrollToHash);
    return () => window.removeEventListener("hashchange", scrollToHash);
  });
</script>

<main>
  <h1>Telemetry Nerd</h1>
  {#if ws.error}<div class="error">{ws.error}</div>{/if}
  <div class="layout">
    <div class="panels">
      {#if panels.length === 0}
        <p class="empty">No panels yet. Ask Claude a question about your metrics.</p>
      {/if}
      {#each panels as panel (panel.id)}
        <PanelView {panel} annotations={ws.snapshot?.annotations ?? []} {threads} />
      {/each}
    </div>
    <Sidebar snapshot={ws.snapshot} />
  </div>
</main>
