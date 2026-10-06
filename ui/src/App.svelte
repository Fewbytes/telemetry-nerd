<script lang="ts">
  import { createWorkspace } from "./lib/workspace.svelte";
  import { theme } from "./lib/theme.svelte";

  // runtime validation instead of a template cast
  const setTheme = (v: string): void => {
    if (v === "light" || v === "dark" || v === "system") theme.set(v);
  };
  import PanelView from "./Panel.svelte";
  import PanelGroupView from "./components/PanelGroup.svelte";
  import { layoutItems } from "./lib/groups";
  import Sidebar from "./components/Sidebar.svelte";
  import ConnectionPill from "./components/ConnectionPill.svelte";
  import WorkspaceSwitcher from "./components/WorkspaceSwitcher.svelte";
  import CatalogView from "./components/CatalogView.svelte";
  import ProposalsView from "./components/ProposalsView.svelte";
  import { fetchProposals, type Proposals } from "./lib/api";
  import { setContext } from "svelte";
  import { refTargets } from "./lib/refs";
  import CodeView from "./components/CodeView.svelte";
  import HighlightStrip from "./components/HighlightStrip.svelte";
  import { scrollIfOffscreen, syncHighlightClasses } from "./lib/refHighlight";
  import type { Highlight } from "./lib/highlights";
  import { downloadWorkspacePdf } from "./lib/exportPdf";

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

  // three views in one page: panels (default), the catalog and the retrospective proposals;
  // evidence links (#/panel/p3, #/finding/f2) go back to the panels
  type Route = "panels" | "catalog" | "proposals";
  const routeOf = (h: string): Route => {
    if (h.startsWith("#/catalog")) return "catalog";
    if (h.startsWith("#/proposals")) return "proposals";
    return "panels";
  };
  let route = $state<Route>(routeOf(location.hash));
  $effect(() => {
    const sync = () => (route = routeOf(location.hash));
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  });

  // proposals are global (they outlive workspaces): fetched on start, on any proposal/lesson
  // event and on a workspace switch (evidence links depend on the workspace on screen)
  let proposals = $state.raw<Proposals | null>(null);
  let proposalsError = $state<string | null>(null);
  const loadProposals = (): Promise<void> =>
    fetchProposals()
      .then((p) => { proposals = p; proposalsError = null; })
      .catch((e) => { proposalsError = String(e); });
  $effect(() => {
    void ws.proposalsSeq;
    void ws.snapshot?.workspace.id;
    loadProposals();
  });
  const pending = $derived(proposals?.pending ?? 0);

  // workspace-to-PDF export (bead w7ht): every open panel's chart plus the question,
  // hypotheses and findings, in one document for sharing/archiving
  let exportBusy = $state(false);
  let exportError = $state<string | null>(null);
  const exportPdf = () => {
    if (!ws.snapshot || exportBusy) return;
    exportBusy = true;
    exportError = null;
    downloadWorkspacePdf(ws.snapshot)
      .catch((e) => (exportError = String(e)))
      .finally(() => (exportBusy = false));
  };

  const panels = $derived((ws.snapshot?.panels ?? []).filter((p) => !p.closed));
  const threads = $derived(ws.snapshot?.threads ?? []); // anchored ones render inside Panel
  // panels of one binding view (bead czt.3) render together as a group
  const items = $derived(layoutItems(panels, ws.snapshot?.groups ?? []));

  $effect(() => {
    const scrollToHash = () => {
      const match = location.hash.match(/^#\/(panel|group|finding)\/(\w+)$/);
      if (match && panels.length > 0) {
        document.getElementById(`${match[1]}-${match[2]}`)?.scrollIntoView();
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
      <a href="#/proposals" aria-current={route === "proposals" ? "page" : undefined} data-proposals-link>
        Proposals{#if pending > 0}<span class="count-badge" data-pending={pending}><span class="sr-only">, </span>{pending}<span class="sr-only"> awaiting review</span></span>{/if}
      </a>
    </nav>
    <div class="header-controls">
      <WorkspaceSwitcher active={ws.snapshot?.workspace ?? null} workspaces={ws.workspaces} onopen={ws.refreshWorkspaces} onsaved={ws.applyWorkspace} />
      <button
        type="button" class="export-pdf" data-export-pdf
        disabled={!ws.snapshot || exportBusy}
        aria-label="Export workspace to PDF"
        onclick={exportPdf}
      >{exportBusy ? "Exporting…" : "Export PDF"}</button>
      <ConnectionPill daemon={ws.daemon} presence={ws.presence} />
      <span class="build-sha" data-build-sha title="UI build commit — confirms what you're looking at is current">{__GIT_SHA__}</span>
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
  {#if exportError}<div class="error" data-export-error role="alert">Export failed: {exportError}</div>{/if}
  <HighlightStrip highlights={ws.highlights} />
  {#if route === "catalog"}<CatalogView />{/if}
  {#if route === "proposals"}
    <ProposalsView data={proposals} active={ws.snapshot?.workspace.id ?? null} error={proposalsError} onreload={loadProposals} />
  {/if}
  <div class="layout" hidden={route !== "panels"}>
    <div class="panels">
      {#if panels.length === 0}
        <p class="empty">No panels yet in {ws.snapshot?.workspace.title ?? "this workspace"}. Ask Claude a question about your metrics.</p>
      {/if}
      {#each items as item (item.kind === "group" ? item.group.id : item.panel.id)}
        {#if item.kind === "group"}
          <PanelGroupView group={item.group} members={item.members} annotations={ws.snapshot?.annotations ?? []} {threads} />
        {:else}
          <PanelView panel={item.panel} annotations={ws.snapshot?.annotations ?? []} {threads} />
        {/if}
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
