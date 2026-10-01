<script lang="ts">
  import { createWorkspace } from "./lib/workspace.svelte";
  import { theme, type Theme } from "./lib/theme.svelte";
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
  <div class="app-header">
    <h1>Telemetry Nerd</h1>
    <select
      class="theme-toggle"
      value={theme.setting}
      aria-label="Theme"
      onchange={(e) => theme.set(e.currentTarget.value as Theme)}
    >
      <option value="system">system</option>
      <option value="light">light</option>
      <option value="dark">dark</option>
    </select>
  </div>
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
