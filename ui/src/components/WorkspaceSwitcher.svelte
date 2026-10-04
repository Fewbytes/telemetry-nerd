<script lang="ts">
  import {
    createWorkspace, fetchWorkspaces, openWorkspace, updateWorkspace, type Snapshot, type WorkspaceInfo,
  } from "../lib/api";
  import { focusAfterRender } from "../lib/focus";
  import { defaultTitle, sortForSwitcher, withWorkspace } from "../lib/workspaces";

  let { active, workspaces, onopen, onsaved }: {
    active: Snapshot["workspace"] | null;
    workspaces: WorkspaceInfo[];
    /** the popover opened: refetch the list so rows, counts and "ago" are current */
    onopen?: () => void;
    /** a create, rename, archive or open landed: the workspace as the daemon saved it */
    onsaved?: (w: WorkspaceInfo) => void;
  } = $props();

  let open = $state(false);
  let showArchived = $state(false);
  // null until the archived fetch lands: the live list stays on screen meanwhile
  let archivedList = $state.raw<WorkspaceInfo[] | null>(null);
  let renaming = $state<string | null>(null);
  let creating = $state(false);
  let error = $state<string | null>(null);
  let root = $state<HTMLElement>();
  let trigger = $state<HTMLButtonElement>();
  let now = $state(Date.now());

  const activeId = $derived(active?.id ?? "");
  // the list is fresher than the snapshot after a rename of the active workspace
  const title = $derived(workspaces.find((w) => w.id === activeId)?.title ?? active?.title ?? "…");
  const rows = $derived(sortForSwitcher(showArchived && archivedList ? archivedList : workspaces, activeId));

  // while open with "Show archived" on, refetch the full list whenever the live one changes;
  // a later request (or closing / switching off) supersedes any response still in flight
  let archivedReq = 0;
  $effect(() => {
    void workspaces;
    const n = ++archivedReq;
    if (!open || !showArchived) { archivedList = null; return; }
    fetchWorkspaces(true)
      .then((l) => { if (n === archivedReq) archivedList = l.workspaces; })
      .catch((e) => { if (n === archivedReq) error = String(e); });
  });

  // "ago" ticks only while the popover is open
  $effect(() => {
    if (!open) return;
    now = Date.now();
    const t = setInterval(() => (now = Date.now()), 30_000);
    return () => clearInterval(t);
  });

  // one request at a time: a double or held Enter must not create two workspaces
  let busy = false;
  const run = (action: () => Promise<WorkspaceInfo>, done?: () => void) => {
    if (busy) return;
    busy = true;
    action()
      .then((w) => {
        error = null;
        if (archivedList) archivedList = withWorkspace(archivedList, w, true);
        onsaved?.(w);
        done?.();
      })
      .catch((e) => (error = String(e)))
      .finally(() => (busy = false));
  };

  const close = () => {
    open = false; renaming = null; creating = false; showArchived = false; error = null;
    trigger?.focus();
  };

  /** an inline edit ended: put focus back on the button that opened it */
  const refocus = (selector: string) => focusAfterRender(() => root?.querySelector<HTMLElement>(selector));

  const ago = (ms: number): string => {
    const s = Math.max(0, (now - ms) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };

  const focusOnMount = (el: HTMLInputElement) => el.focus();

  const create = (e: KeyboardEvent & { currentTarget: HTMLInputElement }) => {
    if (e.key === "Escape") { e.stopPropagation(); creating = false; refocus(".ws-new-btn"); return; }
    if (e.key !== "Enter") return;
    const t = e.currentTarget.value.trim() || defaultTitle(new Date());
    run(() => createWorkspace(t).then((r) => r.workspace), close);
  };

  const rowSel = (id: string, sub: string) => `[data-workspace-id="${CSS.escape(id)}"] ${sub}`;

  const rename = (e: KeyboardEvent & { currentTarget: HTMLInputElement }, w: WorkspaceInfo) => {
    if (e.key === "Escape") { e.stopPropagation(); renaming = null; refocus(rowSel(w.id, ".ws-rename")); return; }
    if (e.key !== "Enter") return;
    const t = e.currentTarget.value.trim();
    const sel = rowSel(w.id, ".ws-rename");
    if (!t || t === w.title) { renaming = null; refocus(sel); return; }
    run(() => updateWorkspace(w.id, { title: t }), () => { renaming = null; refocus(sel); });
  };

  const onkeydown = (e: KeyboardEvent) => {
    if (e.key === "Escape") { close(); return; }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    if ((e.target as HTMLElement).tagName === "INPUT") return;
    const stops = [...(root?.querySelectorAll<HTMLElement>(".ws-open, .ws-new-btn") ?? [])];
    const i = stops.indexOf(document.activeElement as HTMLElement);
    const next = stops[Math.min(stops.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1)))];
    next?.focus();
    e.preventDefault();
  };
</script>

<svelte:window onpointerdown={(e) => { if (open && root && !root.contains(e.target as Node)) close(); }} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="workspace-switcher" bind:this={root} {onkeydown}>
  <button
    type="button" class="ws-trigger" bind:this={trigger} aria-expanded={open} aria-controls="ws-popover"
    onclick={() => { if (open) close(); else { now = Date.now(); open = true; onopen?.(); } }}
  >
    <span class="ws-title">{title}</span><span aria-hidden="true">▾</span>
  </button>
  {#if open}
    <div class="ws-popover" id="ws-popover">
      <ul aria-label="Investigations">
        {#each rows as w (w.id)}
          <li class="ws-row" class:current={w.id === activeId} class:archived={w.archived} data-workspace-id={w.id}>
            {#if renaming === w.id}
              <input
                class="ws-input" aria-label="Rename workspace" value={w.title} maxlength="120" use:focusOnMount
                onkeydown={(e) => rename(e, w)} onblur={() => (renaming = null)}
              />
            {:else}
              <button type="button" class="ws-open" title={w.question ?? undefined} aria-current={w.id === activeId ? "true" : undefined} onclick={() => run(() => openWorkspace(w.id).then((r) => r.workspace), close)}>
                <span class="ws-name">{w.title}</span>
                <span class="ws-meta">
                  {#if w.archived}archived · {/if}{ago(w.last_activity_ms)} · {w.counts.findings} finding{w.counts.findings === 1 ? "" : "s"}
                </span>
              </button>
              <button type="button" class="ws-act ws-rename" aria-label="Rename {w.title}" onclick={() => (renaming = w.id)}>Rename</button>
              {#if w.id !== activeId && !w.archived}
                <button type="button" class="ws-act" aria-label="Archive {w.title}" onclick={() => run(() => updateWorkspace(w.id, { archived: true }))}>Archive</button>
              {/if}
            {/if}
          </li>
        {/each}
      </ul>
      {#if creating}
        <input
          class="ws-input" aria-label="New investigation title" maxlength="120" placeholder={defaultTitle(new Date())}
          use:focusOnMount onkeydown={create}
        />
      {:else}
        <button type="button" class="ws-new-btn" onclick={() => (creating = true)}>New investigation</button>
      {/if}
      <label class="ws-archived"><input type="checkbox" bind:checked={showArchived} /> Show archived</label>
      {#if error}<p class="ws-error" role="alert">{error}</p>{/if}
    </div>
  {/if}
</div>

<style>
  .workspace-switcher { position: relative; }
  .ws-trigger {
    display: inline-flex; align-items: center; gap: 6px; font: inherit; font-size: 13px; max-width: 280px;
    background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; cursor: pointer;
  }
  .ws-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ws-popover {
    position: absolute; right: 0; top: calc(100% + 6px); z-index: 10; width: min(420px, calc(100vw - 32px));
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 8px;
    font-size: 13px; box-shadow: 0 4px 16px rgb(0 0 0 / 0.15);
  }
  ul { list-style: none; margin: 0 0 6px; padding: 0; max-height: 50vh; overflow-y: auto; }
  .ws-row { display: flex; align-items: center; gap: 4px; padding: 2px 0; }
  .ws-row.current .ws-name { font-weight: 600; }
  .ws-row.archived { opacity: 0.7; }
  .ws-open {
    flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; text-align: left;
    font: inherit; background: none; color: var(--fg); border: 0; padding: 4px 6px; cursor: pointer; border-radius: 4px;
  }
  .ws-open:hover, .ws-act:hover, .ws-new-btn:hover { background: var(--code-bg); }
  .ws-name { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ws-meta { font-size: 11px; color: var(--muted); }
  .ws-act, .ws-new-btn {
    font: inherit; font-size: 12px; background: none; color: var(--muted); border: 1px solid var(--border);
    border-radius: 4px; padding: 2px 6px; cursor: pointer;
  }
  .ws-new-btn { width: 100%; text-align: left; color: var(--fg); }
  .ws-input {
    width: 100%; box-sizing: border-box; font: inherit; background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 4px; padding: 3px 6px;
  }
  .ws-archived { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 12px; color: var(--muted); }
  .ws-error { margin: 6px 0 0; font-size: 12px; color: var(--error); }
</style>
