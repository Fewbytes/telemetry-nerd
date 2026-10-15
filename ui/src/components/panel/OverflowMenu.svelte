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
    type="button" bind:this={trigger} aria-haspopup="true" aria-expanded={open} aria-label={label}
    onclick={() => (open = !open)}
  >⋯</button>
  {#if open}
    <span class="overflow-pop" role="group" aria-label={label}>{@render children()}</span>
  {/if}
</span>
