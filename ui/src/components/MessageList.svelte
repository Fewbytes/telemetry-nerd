<script lang="ts">
  import { getContext } from "svelte";
  import type { Presence, Thread } from "../lib/api";
  import { messageStatus } from "../lib/connection";
  import RefText from "./RefText.svelte";

  let { thread }: { thread: Thread } = $props();
  const presence = getContext<(() => Presence | null) | undefined>("presence") ?? (() => null);
</script>

<ul class="messages">
  {#each thread.messages as m, i (m.id)}
    {@const status = messageStatus(thread, i, presence())}
    <li class="message">
      <span class="badge author {m.author}">{m.author}</span>
      <span class="text"><RefText text={m.text} /></span>
      {#if status === "delivered"}
        <span class="delivery processing" data-testid="delivery-{m.id}">
          <span class="spinner" aria-hidden="true"></span>processing
        </span>
      {:else if status}
        <span class="delivery" data-testid="delivery-{m.id}">{status}</span>
      {/if}
    </li>
  {/each}
</ul>
