<script lang="ts">
  import { postJSON, type Thread } from "../lib/api";
  import { fmtTime } from "../lib/format";

  let { thread }: { thread: Thread } = $props();

  let text = $state("");
  let busy = $state(false);
  let error = $state<string | null>(null);

  const send = () => {
    const t = text.trim();
    if (!t) return;
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
    {#each thread.messages as m (m.id)}
      <li class="message">
        <span class="badge author {m.author}">{m.author}</span>
        <span class="text">{m.text}</span>
      </li>
    {/each}
  </ul>
  <div class="reply">
    <textarea
      bind:value={text}
      rows="2"
      placeholder="Reply…"
      aria-label="Reply to thread {thread.id}"
    ></textarea>
    <button type="button" disabled={busy || !text.trim()} onclick={send}>Send</button>
  </div>
  {#if error}<div class="error">{error}</div>{/if}
</section>
