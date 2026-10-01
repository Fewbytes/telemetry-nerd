<script lang="ts">
  import { getContext } from "svelte";
  import { splitRefs, type RefTarget } from "../lib/refs";

  let { text }: { text: string } = $props();
  const targets = getContext<(() => Map<string, RefTarget>) | undefined>("refs") ?? (() => new Map());
  const segments = $derived(splitRefs(text, targets()));

  const el = (t: RefTarget) => (t.domId ? document.getElementById(t.domId) : null);
  const on = (t: RefTarget) => el(t)?.classList.add("ref-hover");
  const off = (t: RefTarget) => el(t)?.classList.remove("ref-hover");
  const go = (t: RefTarget) => {
    const target = el(t);
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.classList.remove("ref-flash");
    void target.offsetWidth; // restart the animation on repeat clicks
    target.classList.add("ref-flash");
    target.addEventListener("animationend", () => target.classList.remove("ref-flash"), { once: true });
  };
</script>

{#each segments as seg}
  {#if "ref" in seg}
    <button
      type="button"
      class="ref-chip obj-id"
      class:closed={seg.closed}
      data-ref={seg.ref}
      title={seg.closed ? `${seg.label} (panel closed)` : seg.label}
      onmouseenter={() => on(seg)}
      onmouseleave={() => off(seg)}
      onfocus={() => on(seg)}
      onblur={() => off(seg)}
      onclick={() => go(seg)}>{seg.ref}</button
    >
  {:else}{seg.text}{/if}
{/each}
