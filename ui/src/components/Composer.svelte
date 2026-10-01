<script lang="ts">
  import { isSendKey, sendHint } from "../lib/keys";

  let { placeholder, label, onsend }: {
    placeholder: string;
    label: string;
    onsend: (text: string) => Promise<unknown>;
  } = $props();

  let text = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const send = () => {
    const t = text.trim();
    if (busy || !t) return;
    busy = true;
    error = null;
    onsend(t)
      .then(() => (text = ""))
      .catch((e) => (error = String(e)))
      .finally(() => (busy = false));
  };
</script>

<div class="reply">
  <textarea
    bind:value={text}
    rows="2"
    placeholder={sendHint() + placeholder}
    aria-label={label}
    onkeydown={(e) => {
      if (isSendKey(e)) {
        e.preventDefault();
        send();
      }
    }}
  ></textarea>
  <button type="button" disabled={busy || !text.trim()} onclick={send}>Send</button>
</div>
{#if error}<div class="error">{error}</div>{/if}
