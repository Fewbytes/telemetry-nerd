<script lang="ts">
  import { getContext } from "svelte";
  import { postJSON, type Presence, type Thread } from "../lib/api";
  import { composeHint } from "../lib/connection";
  import { fmtRange } from "../lib/format";
  import Composer from "./Composer.svelte";
  import MessageList from "./MessageList.svelte";

  let { thread }: { thread: Thread } = $props();
  const presence = getContext<(() => Presence | null) | undefined>("presence") ?? (() => null);
</script>

<section class="thread" id="thread-{thread.id}">
  <header>
    <span class="thread-id">{thread.id}</span>
    {#if thread.selection}
      <span class="selection">
        selection {fmtRange(thread.selection.start_ms, thread.selection.end_ms)} UTC
      </span>
    {/if}
  </header>
  <MessageList {thread} />
  <Composer
    placeholder={composeHint(presence())}
    label="Reply to thread {thread.id}"
    onsend={(text) => postJSON(`/api/threads/${thread.id}/messages`, { text })}
  />
</section>
