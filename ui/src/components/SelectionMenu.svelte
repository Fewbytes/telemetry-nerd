<script lang="ts">
  import { postJSON } from "../lib/api";
  import { isSendKey, sendHint } from "../lib/keys";

  let {
    panelId,
    x0,
    x1,
    left,
    top,
    width,
    onCancel,
  }: {
    panelId: string;
    x0: number; // selection start, seconds
    x1: number; // selection end, seconds
    left: number; // px, relative to the plot container
    top: number; // px, relative to the plot container
    width: number; // px of the brush selection
    onCancel: () => void;
  } = $props();

  // seconds → epoch ms, rounded ($derived: props can update while the menu is open)
  const startMs = $derived(Math.round(x0 * 1000));
  const endMs = $derived(Math.round(x1 * 1000));

  let menuEl = $state<HTMLDivElement | null>(null);
  let mode = $state<"actions" | "ask" | "region">("actions");
  let text = $state("");
  let label = $state("");
  let error = $state<string | null>(null);
  let busy = $state(false);

  const run = (p: Promise<unknown>) => {
    busy = true;
    error = null;
    p.then(() => onCancel())
      .catch((e) => (error = String(e)))
      .finally(() => (busy = false));
  };

  const ask = () =>
    run(postJSON("/api/threads", { text, anchor: panelId, selection: { start_ms: startMs, end_ms: endMs } }));
  const markRegion = () =>
    run(postJSON("/api/annotations", {
      kind: "region", panel: panelId, t_start_ms: startMs, t_end_ms: endMs, label,
    }));
  const markEvent = () =>
    run(postJSON("/api/annotations", { kind: "event", panel: panelId, t_start_ms: startMs }));
  const focus = () => run(postJSON("/api/focus", { start_ms: startMs, end_ms: endMs }));

  $effect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    const onDown = (e: MouseEvent) => {
      if (menuEl && !menuEl.contains(e.target as Node)) onCancel();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  });
</script>

<div
  bind:this={menuEl}
  class="selection-menu"
  style="left: {left}px; top: {top}px; width: {Math.max(width, 220)}px;"
>
  {#if mode === "actions"}
    <button type="button" onclick={() => (mode = "ask")}>Ask Claude…</button>
    <button type="button" onclick={() => (mode = "region")}>Mark region</button>
    <button type="button" disabled={busy} onclick={markEvent}>Mark event</button>
    <button type="button" disabled={busy} onclick={focus}>Focus</button>
  {:else if mode === "ask"}
    <textarea
      bind:value={text}
      placeholder="Ask Claude about this selection — {sendHint()}"
      rows="3"
      onkeydown={(e) => {
        if (!isSendKey(e)) return;
        e.preventDefault();
        if (!busy && text.trim()) ask();
      }}
    ></textarea>
    <div class="row">
      <button type="button" disabled={busy || !text.trim()} onclick={ask}>Send</button>
      <button type="button" onclick={() => (mode = "actions")}>Back</button>
    </div>
  {:else}
    <input
      bind:value={label}
      placeholder="Region label"
      onkeydown={(e) => {
        if (!isSendKey(e)) return;
        e.preventDefault();
        if (!busy) markRegion();
      }}
    />
    <div class="row">
      <button type="button" disabled={busy} onclick={markRegion}>Save</button>
      <button type="button" onclick={() => (mode = "actions")}>Back</button>
    </div>
  {/if}
  {#if error}<div class="menu-error">{error}</div>{/if}
</div>

<style>
  .selection-menu {
    position: absolute;
    z-index: 10;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 6px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  }
  .selection-menu button {
    text-align: left;
    background: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 3px 8px;
    cursor: pointer;
    color: var(--fg);
  }
  .selection-menu textarea,
  .selection-menu input {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    color: var(--fg);
    font: inherit;
  }
  .selection-menu .row {
    display: flex;
    gap: 4px;
  }
  .menu-error {
    color: var(--error);
    font-size: 12px;
  }
</style>