<script lang="ts">
  import { getContext } from "svelte";
  import { postJSON, type Presence, type Thread } from "../lib/api";
  import { composeHint, messageStatus } from "../lib/connection";
  import { fmtTime } from "../lib/format";
  import { isSendKey, sendHint } from "../lib/keys";

  let { thread }: { thread: Thread } = $props();
  const presence = getContext<(() => Presence | null) | undefined>("presence") ?? (() => null);

  let text = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const send = () => {
    const t = text.trim();
    if (busy || !t) return;
    busy = true;
    error = null;
    postJSON(`/api/threads/${thread.id}/messages`, { text: t })
      .then(() => (text = ""))
      .catch((e) => (error = String(e)))
      .finally(() => (busy = false));
  };
</script>

<section class="thread" id="thread-{thread.id}">
  <header>
    <span class="thread-id">{thread.id}</span>
    {#if thread.selection}
      <span class="selection">
        selection {fmtTime(thread.selection.start_ms)}–{fmtTime(thread.selection.end_ms)} UTC
      </span>
    {/if}
  </header>
  <ul class="messages">
    {#each thread.messages as m, i (m.id)}
      {@const status = messageStatus(thread, i, presence())}
      <li class="message">
        <span class="badge author {m.author}">{m.author}</span>
        <span class="text">{m.text}</span>
        {#if status}<span class="delivery" data-testid="delivery-{m.id}">{status}</span>{/if}
      </li>
    {/each}
  </ul>
  <div class="reply">
    <textarea
      bind:value={text}
      rows="2"
      placeholder={sendHint() + composeHint(presence())}
      aria-label="Reply to thread {thread.id}"
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
</section>
