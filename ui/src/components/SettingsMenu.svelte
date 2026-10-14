<script lang="ts">
  import { mode, type Mode } from "../lib/mode.svelte";

  let open = $state(false);
  let root = $state<HTMLElement>();

  $effect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root && !root.contains(e.target as Node)) open = false;
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && (open = false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  });
</script>

<span class="settings" bind:this={root}>
  <button type="button" aria-haspopup="true" aria-expanded={open} onclick={() => (open = !open)}>Settings</button>
  {#if open}
    <div class="pop" role="dialog" aria-label="Settings">
      <label>
        Default mode
        <select value={mode.default} onchange={(e) => mode.setDefault(e.currentTarget.value as Mode)}>
          <option value="simple">simple</option>
          <option value="nerd">nerd</option>
        </select>
      </label>
      <p class="hint">Simple: minimal panels. Nerd: everything. The header toggle overrides this for the current session.</p>
    </div>
  {/if}
</span>

<style>
  .settings { position: relative; display: inline-block; }
  .pop { position: absolute; right: 0; top: 100%; z-index: 20; min-width: 16rem; padding: 0.75rem; background: var(--bg); border: 1px solid var(--grid); border-radius: 6px; }
  .hint { color: var(--muted); font-size: 0.85em; margin: 0.5rem 0 0; }
</style>
