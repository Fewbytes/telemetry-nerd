<script lang="ts">
  import {
    createWorkspace, fetchWorkspaces, openWorkspace, updateWorkspace, type Snapshot, type WorkspaceInfo,
  } from "../lib/api";
  import { defaultTitle, sortForSwitcher } from "../lib/workspaces";

  let { active, workspaces, onopen }: {
    active: Snapshot["workspace"] | null;
    workspaces: WorkspaceInfo[];
    /** the popover opened: refetch the list so rows, counts and "ago" are current */
    onopen?: () => void;
  } = $props();

  let open = $state(false);
  let showArchived = $state(false);
  let archivedList = $state.raw<WorkspaceInfo[]>([]);
  let renaming = $state<string | null>(null);
  let creating = $state(false);
  let error = $state<string | null>(null);
  let root = $state<HTMLElement>();

  const activeId = $derived(active?.id ?? "");
  // the list is fresher than the snapshot after a rename of the active workspace
  const title = $derived(workspaces.find((w) => w.id === activeId)?.title ?? active?.title ?? "…");
  const rows = $derived(sortForSwitcher(showArchived ? archivedList : workspaces, activeId));

  // with "Show archived" on, refetch the full list whenever the live one changes
  $effect(() => {
    void workspaces;
    if (!showArchived) return;
    fetchWorkspaces(true).then((l) => (archivedList = l.workspaces)).catch((e) => (error = String(e)));
  });

  // one request at a time: a double or held Enter must not create two workspaces
  let busy = false;
  const run = (action: () => Promise<unknown>, done?: () => void) => {
    if (busy) return;
    busy = true;
    action()
      .then(() => { error = null; done?.(); })
      .catch((e) => (error = String(e)))
      .finally(() => (busy = false));
  };

  const close = () => { open = false; renaming = null; creating = false; error = null; };

  const ago = (ms: number): string => {
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };

  const focusOnMount = (el: HTMLInputElement) => el.focus();

  const create = (e: KeyboardEvent & { currentTarget: HTMLInputElement }) => {
    if (e.key === "Escape") { e.stopPropagation(); creating = false; return; }
    if (e.key !== "Enter") return;
    const t = e.currentTarget.value.trim() || defaultTitle(new Date());
    run(() => createWorkspace(t), close);
  };

  const rename = (e: KeyboardEvent & { currentTarget: HTMLInputElement }, w: WorkspaceInfo) => {
    if (e.key === "Escape") { e.stopPropagation(); renaming = null; return; }
    if (e.key !== "Enter") return;
    const t = e.currentTarget.value.trim();
    if (!t || t === w.title) { renaming = null; return; }
    run(() => updateWorkspace(w.id, { title: t }), () => (renaming = null));
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
    type="button" class="ws-trigger" aria-haspopup="listbox" aria-expanded={open}
    onclick={() => { if (open) close(); else { open = true; onopen?.(); } }}
  >
    <span class="ws-title">{title}</span><span aria-hidden="true">▾</span>
  </button>
  {#if open}
    <div class="ws-popover">
      <ul role="listbox" aria-label="Investigations">
        {#each rows as w (w.id)}
          <li role="option" aria-selected={w.id === activeId} class="ws-row" class:archived={w.archived} data-workspace-id={w.id}>
            {#if renaming === w.id}
              <input
                class="ws-input" aria-label="Rename workspace" value={w.title} use:focusOnMount
                onkeydown={(e) => rename(e, w)} onblur={() => (renaming = null)}
              />
            {:else}
              <button type="button" class="ws-open" title={w.question ?? undefined} aria-label="Open {w.title}" onclick={() => run(() => openWorkspace(w.id), close)}>
                <span class="ws-name">{w.title}</span>
                <span class="ws-meta">
                  {#if w.archived}archived · {/if}{ago(w.last_activity_ms)} · {w.counts.findings} finding{w.counts.findings === 1 ? "" : "s"}
                </span>
              </button>
              <button type="button" class="ws-act" aria-label="Rename {w.title}" onclick={() => (renaming = w.id)}>Rename</button>
              {#if w.id !== activeId && !w.archived}
                <button type="button" class="ws-act" aria-label="Archive {w.title}" onclick={() => run(() => updateWorkspace(w.id, { archived: true }))}>Archive</button>
              {/if}
            {/if}
          </li>
        {/each}
      </ul>
      {#if creating}
        <input
          class="ws-input" aria-label="New investigation title" placeholder={defaultTitle(new Date())}
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
  .ws-row[aria-selected="true"] .ws-name { font-weight: 600; }
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
