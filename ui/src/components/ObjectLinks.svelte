<script lang="ts">
  import { getContext } from "svelte";
  import type { ObjectLink } from "../lib/findings";
  import { flash, hoverOff, hoverOn } from "../lib/refHighlight";

  let { links }: { links: ObjectLink[] } = $props();
  const openCode = getContext<((id: string) => void) | undefined>("openCode");
</script>

{#each links as l (l.kind + l.id)}
  {#if l.kind === "catalog"}
    <a class="obj-link" href="#/catalog" title="open the catalog to see the claims side by side">{l.label}</a>
  {:else if l.kind === "code"}
    <button type="button" class="obj-link" aria-label="Open code run {l.id}" onclick={() => openCode?.(l.id)}>{l.label}</button>
  {:else if l.domId}
    <button
      type="button"
      class="obj-link"
      aria-label="Show {l.kind} {l.id}"
      onmouseenter={() => hoverOn(l.domId)}
      onmouseleave={() => hoverOff(l.domId)}
      onfocus={() => hoverOn(l.domId)}
      onblur={() => hoverOff(l.domId)}
      onclick={() => flash(l.domId)}>{l.label}</button>
  {:else}
    <span class="obj-link plain">{l.label}</span>
  {/if}
{/each}
