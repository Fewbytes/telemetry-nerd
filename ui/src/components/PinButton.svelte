<script lang="ts">
  import { getContext } from "svelte";
  import { clearHighlight, pinHighlight } from "../lib/api";
  import type { Highlights } from "../lib/highlights";
  import { isSendKey, sendHint } from "../lib/keys";

  let { object }: { object: string } = $props();
  const highlights = getContext<(() => Highlights) | undefined>("highlights") ?? (() => new Map());
  const active = $derived(highlights().has(object));

  let open = $state(false);
  let note = $state("");
  let error = $state<string | null>(null);

  const pin = () => {
    error = null;
    pinHighlight(object, note.trim())
      .then(() => {
        open = false;
        note = "";
      })
      .catch((e) => (error = String(e)));
  };
  const clear = () => clearHighlight(object).catch((e) => (error = String(e)));
</script>

<span class="pin">
  <button
    type="button"
    class="pin-btn"
    class:active
    aria-pressed={active}
    aria-label={active ? `Clear highlight on ${object}` : `Highlight ${object}`}
    title={active ? "Clear highlight" : "Highlight (optionally tell Claude why)"}
    onclick={() => (active ? clear() : (open = !open))}>📍</button
  >
  {#if open && !active}
    <span class="pin-note">
      <input
        type="text"
        bind:value={note}
        placeholder={`note for Claude (optional) — ${sendHint()}`}
        aria-label="Highlight note for {object}"
        onkeydown={(e) => {
          if (e.key === "Enter" || isSendKey(e)) {
            e.preventDefault();
            pin();
          } else if (e.key === "Escape") open = false;
        }}
      />
      <button type="button" onclick={pin}>Pin</button>
    </span>
  {/if}
  {#if error}<span class="error">{error}</span>{/if}
</span>
