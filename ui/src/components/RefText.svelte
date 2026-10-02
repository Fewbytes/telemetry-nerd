<script lang="ts">
  import { getContext } from "svelte";
  import { flash, hoverOff, hoverOn } from "../lib/refHighlight";
  import { splitRefs, type RefTarget } from "../lib/refs";

  let { text }: { text: string } = $props();
  const targets = getContext<(() => Map<string, RefTarget>) | undefined>("refs") ?? (() => new Map());
  const openCode = getContext<((id: string) => void) | undefined>("openCode");
  const segments = $derived(splitRefs(text, targets()));
</script>

{#each segments as seg}
  {#if "ref" in seg}
    <button
      type="button"
      class="ref-chip obj-id"
      class:closed={seg.closed}
      data-ref={seg.ref}
      title={seg.closed ? `${seg.label} (panel closed)` : seg.label}
      onmouseenter={() => hoverOn(seg.domId)}
      onmouseleave={() => hoverOff(seg.domId)}
      onfocus={() => hoverOn(seg.domId)}
      onblur={() => hoverOff(seg.domId)}
      onclick={() => (seg.kind === "code" ? openCode?.(seg.id) : flash(seg.domId))}>{seg.ref}</button
    >
  {:else}{seg.text}{/if}
{/each}
