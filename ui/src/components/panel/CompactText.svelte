<script lang="ts">
  // simple mode's form of a dense caption: one ellipsised line, click (or Enter) opens it in place;
  // the full text is always in the DOM and the title, so the numbers keep their basis (P4, P12).
  import type { Snippet } from "svelte";

  let { text, label, children }: {
    /** the full caption: the title, and the content unless a snippet renders it */
    text: string;
    /** read before the text by assistive tech, e.g. "group binding" */
    label?: string;
    /** rich content in place of the plain text (no interactive elements: it renders inside a button) */
    children?: Snippet;
  } = $props();
  let open = $state(false);
</script>

<button type="button" class="compact-text" class:open data-compact-text aria-expanded={open} title={text} onclick={() => (open = !open)}
>{#if label}<span class="sr-only">{label}: </span>{/if}{#if children}{@render children()}{:else}{text}{/if}</button>
