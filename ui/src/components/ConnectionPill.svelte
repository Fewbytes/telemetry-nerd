<script lang="ts">
  import type { Presence } from "../lib/api";
  import { pill, type DaemonState } from "../lib/connection";

  let { daemon, presence }: { daemon: DaemonState; presence: Presence | null } = $props();

  const view = $derived(pill(daemon, presence));
  let open = $state(false);
  let copied = $state(false);

  const copy = (cmd: string) =>
    navigator.clipboard
      ?.writeText(cmd)
      .then(() => {
        copied = true;
        setTimeout(() => (copied = false), 1500);
      })
      .catch(() => {});
</script>

<div class="connection">
  <button
    type="button"
    class="pill {view.tone}"
    aria-expanded={open}
    aria-controls="connection-detail"
    onclick={() => (open = !open)}
  >
    <span class="dot" aria-hidden="true"></span>
    <span aria-live="polite" data-testid="connection-label">{view.label}</span>
  </button>
  {#if open}
    <div class="connection-detail" id="connection-detail" role="status">
      <p>{view.detail}</p>
      {#if view.command}
        <div class="command">
          <code>{view.command}</code>
          <button type="button" onclick={() => copy(view.command!)}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      {/if}
    </div>
  {/if}
</div>
