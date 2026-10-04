<script lang="ts">
  import { getContext } from "svelte";
  import { clearHighlight } from "../lib/api";
  import type { Highlights } from "../lib/highlights";
  import { flash, hoverOff, hoverOn } from "../lib/refHighlight";
  import type { RefTarget } from "../lib/refs";

  let { highlights }: { highlights: Highlights } = $props();
  const targets = getContext<(() => Map<string, RefTarget>) | undefined>("refs") ?? (() => new Map());
  const entries = $derived([...highlights.values()]);

  const clear = (id: string) => clearHighlight(id).catch(() => {});
</script>

{#if entries.length > 0}
  <ul class="highlight-strip" aria-label="Highlighted">
    {#each entries as h (h.id)}
      {@const t = targets().get(h.id)}
      <li class="highlight-item" data-highlight-author={h.author} data-id={h.id}>
        <span class="badge author {h.author}">{h.author}</span>
        <button
          type="button"
          class="ref-chip obj-id"
          title={t?.label}
          onmouseenter={() => hoverOn(t?.domId ?? null)}
          onmouseleave={() => hoverOff(t?.domId ?? null)}
          onclick={() => flash(t?.domId ?? null)}>{h.id}</button
        >
        {#if h.note}<span class="note">{h.note}</span>{/if}
        <button type="button" class="dismiss" aria-label="Clear highlight on {h.id}" onclick={() => clear(h.id)}>×</button>
      </li>
    {/each}
  </ul>
{/if}
