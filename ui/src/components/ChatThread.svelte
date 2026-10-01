<script lang="ts">
  import { getContext } from "svelte";
  import { postJSON, type Presence, type Thread } from "../lib/api";
  import { composeHint } from "../lib/connection";
  import Composer from "./Composer.svelte";
  import MessageList from "./MessageList.svelte";

  let { thread }: { thread: Thread | null } = $props();
  const presence = getContext<(() => Presence | null) | undefined>("presence") ?? (() => null);

  // first message creates the anchor-less thread; later ones append to it
  const send = (text: string) =>
    thread
      ? postJSON(`/api/threads/${thread.id}/messages`, { text })
      : postJSON("/api/threads", { text, anchor: null });

  let listEl: HTMLDivElement | undefined = $state();
  $effect(() => {
    void thread?.messages.length;
    listEl?.scrollTo({ top: listEl.scrollHeight });
  });
</script>

<div class="chat" id="chat">
  {#if thread && thread.messages.length > 0}
    <div class="chat-scroll" bind:this={listEl}><MessageList {thread} /></div>
  {:else}
    <p class="none">Ask Claude anything about this investigation.</p>
  {/if}
  <Composer
    placeholder={composeHint(presence())}
    label="Chat with Claude"
    onsend={send}
  />
</div>
