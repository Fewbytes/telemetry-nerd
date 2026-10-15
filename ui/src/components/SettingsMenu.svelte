<script lang="ts">
  import { mode } from "../lib/mode.svelte";

  let open = $state(false);
  let root = $state<HTMLElement>();
  let trigger = $state<HTMLButtonElement>();
  let pop = $state<HTMLElement>();

  $effect(() => {
    if (!open) return;
    // focus moves into the dialog on open: the checked default, the one control it holds
    pop?.querySelector<HTMLElement>("input:checked")?.focus();
    const onDown = (e: MouseEvent) => {
      if (root && !root.contains(e.target as Node)) open = false;
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      open = false;
      trigger?.focus(); // Esc returns focus to where the dialog was opened from
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  });
</script>

<span class="settings" bind:this={root}>
  <button type="button" bind:this={trigger} aria-haspopup="dialog" aria-expanded={open} onclick={() => (open = !open)}>Settings</button>
  {#if open}
    <div class="pop" role="dialog" bind:this={pop} aria-label="Settings">
      <!-- radios, not a select: re-choosing the current default fires click but no change, and it must still clear the session override -->
      <div role="radiogroup" aria-label="Default mode" class="modes">
        <span>Default mode</span>
        {#each ["simple", "nerd"] as const as m}
          <label>
            <input type="radio" name="default-mode" value={m} checked={mode.default === m} onclick={() => mode.setDefault(m)} />
            {m}
          </label>
        {/each}
      </div>
      <p class="hint">Simple: minimal panels. Nerd: everything. The header toggle overrides this for the current session.</p>
    </div>
  {/if}
</span>

<style>
  .settings { position: relative; display: inline-block; }
  .pop { position: absolute; right: 0; top: 100%; z-index: 20; min-width: 16rem; max-width: calc(100vw - 1.5rem); box-sizing: border-box; padding: 0.75rem; background: var(--bg); color: var(--fg); border: 1px solid var(--border); box-shadow: 0 2px 8px rgb(0 0 0 / 0.25); border-radius: 6px; }
  .modes { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  .modes > span { flex-basis: 100%; }
  .hint { color: var(--muted); font-size: 0.85em; margin: 0.5rem 0 0; }
</style>
