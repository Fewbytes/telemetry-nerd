<script lang="ts">
  // simple mode's form of a dense caption: one ellipsised line, click (or Enter) opens it in place;
  // the full text is always in the DOM and the title, so the numbers keep their basis (P4, P12).
  let { text }: { text: string } = $props();
  let open = $state(false);
</script>

<button type="button" class="compact-text" class:open data-compact-text aria-expanded={open} title={text} onclick={() => (open = !open)}
>{text}</button>
