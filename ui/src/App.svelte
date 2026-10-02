<script lang="ts">
  import { createWorkspace } from "./lib/workspace.svelte";
  import { theme } from "./lib/theme.svelte";

  // runtime validation instead of a template cast
  const setTheme = (v: string): void => {
    if (v === "light" || v === "dark" || v === "system") theme.set(v);
  };
  import PanelView from "./Panel.svelte";
  import Sidebar from "./components/Sidebar.svelte";
  import ConnectionPill from "./components/ConnectionPill.svelte";
  import CatalogView from "./components/CatalogView.svelte";
  import { setContext } from "svelte";
  import { refTargets } from "./lib/refs";
  import CodeView from "./components/CodeView.svelte";
  import HighlightStrip from "./components/HighlightStrip.svelte";
  import { scrollIfOffscreen, syncHighlightClasses } from "./lib/refHighlight";
  import type { Highlight } from "./lib/highlights";

  const ws = createWorkspace();
  // threads read presence for per-message delivery state without prop drilling
  setContext("presence", () => ws.presence);
  // message text resolves object ids (p3, f2) to hover/click chips
  setContext("refs", () => (ws.snapshot ? refTargets(ws.snapshot) : new Map()));
  setContext("highlights", () => ws.highlights);
  setContext("catalogSeq", () => ws.catalogSeq);
  // the read-only code view (tier-2 nodes): opened from panel provenance, the run list and c-id chips
  let codeOpen = $state<string | null>(null);
  const openCode = (id: string): void => { codeOpen = id; };
  setContext("openCode", openCode);
  $effect(() => ws.start());

  // accent every highlighted target; re-runs on snapshot change so re-rendered DOM keeps it
  let seen = new Map<string, Highlight>();
  $effect(() => {
    const targets = ws.snapshot ? refTargets(ws.snapshot) : new Map();
    const current = ws.highlights;
    syncHighlightClasses(
      [...current.values()].map((h) => ({ domId: targets.get(h.id)?.domId ?? null, author: h.author })),
    );
    // bring a new Claude highlight into view once; the user's own pins are where they already are
    for (const h of current.values()) {
      if (seen.get(h.id) !== h && h.author === "claude") scrollIfOffscreen(targets.get(h.id)?.domId ?? null);
    }
    seen = new Map(current);
  });

  // two views in one page: panels (default) and the catalog; evidence links (#/panel/p3) go back
  let route = $state<"panels" | "catalog">(location.hash.startsWith("#/catalog") ? "catalog" : "panels");
  $effect(() => {
    const sync = () => (route = location.hash.startsWith("#/catalog") ? "catalog" : "panels");
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  });

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
    <h1 class="brand"><img src="/icon.svg" alt="" width="32" height="32" />Telemetry Nerd</h1>
    <nav class="views" aria-label="Views">
      <a href="#/panels" aria-current={route === "panels" ? "page" : undefined}>Panels</a>
      <a href="#/catalog" aria-current={route === "catalog" ? "page" : undefined}>Catalog</a>
    </nav>
    <div class="header-controls">
      <ConnectionPill daemon={ws.daemon} presence={ws.presence} />
      <select
        class="theme-toggle"
        value={theme.setting}
        aria-label="Theme"
        onchange={(e) => setTheme(e.currentTarget.value)}
      >
        <option value="system">system</option>
        <option value="light">light</option>
        <option value="dark">dark</option>
      </select>
    </div>
  </div>
  {#if ws.error}<div class="error">{ws.error}</div>{/if}
  <HighlightStrip highlights={ws.highlights} />
  {#if route === "catalog"}<CatalogView />{/if}
  <div class="layout" hidden={route === "catalog"}>
    <div class="panels">
      {#if panels.length === 0}
        <p class="empty">No panels yet. Ask Claude a question about your metrics.</p>
      {/if}
      {#each panels as panel (panel.id)}
        <PanelView {panel} annotations={ws.snapshot?.annotations ?? []} {threads} />
      {/each}
    </div>
    <Sidebar snapshot={ws.snapshot} onopencode={openCode} />
  </div>
  {#if codeOpen}
    <CodeView
      id={codeOpen} runs={ws.snapshot?.code ?? []} panels={ws.snapshot?.panels ?? []}
      onclose={() => (codeOpen = null)} onopen={openCode}
    />
  {/if}
</main>
