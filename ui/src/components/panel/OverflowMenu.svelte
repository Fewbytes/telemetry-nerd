<script lang="ts">
  import type { Snippet } from "svelte";

  let { label, children }: { label: string; children: Snippet } = $props();
  let open = $state(false);
  let root = $state<HTMLElement>();
  let trigger = $state<HTMLButtonElement>();

  $effect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root && !root.contains(e.target as Node)) open = false;
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      open = false;
      trigger?.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  });
</script>

<span class="overflow" bind:this={root}>
  <button
    type="button" bind:this={trigger} aria-expanded={open} aria-label={label}
    onclick={() => (open = !open)}
  >⋯</button>
  {#if open}
    <!-- an item's own click handler runs first, then the menu closes -->
    <!-- svelte-ignore a11y_click_events_have_key_events (the items are buttons: Enter/Space on one is a click that bubbles here) -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions (same: this only observes its items' clicks) -->
    <span class="overflow-pop" role="group" aria-label={label} onclick={() => queueMicrotask(() => (open = false))}>{@render children()}</span>
  {/if}
</span>
