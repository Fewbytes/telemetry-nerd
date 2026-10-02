<script lang="ts">
  // Activity list of tier-2 runs: newest first, status in words + glyph, click opens the code view.
  import type { CodeBrief } from "../lib/api";
  import { newestFirst, runSummary, statusView } from "../lib/codeView";

  let { runs, onopen }: { runs: CodeBrief[]; onopen: (id: string) => void } = $props();
  const shown = $derived(newestFirst(runs));
  let all = $state(false);
  const LIMIT = 8;
</script>

<ul class="code-runs">
  {#each all ? shown : shown.slice(0, LIMIT) as c (c.id)}
    {@const st = statusView(c)}
    <li id="code-{c.id}" class="run {st.tone}" data-code-run={c.id}>
      <button type="button" class="run-btn" onclick={() => onopen(c.id)} aria-label="View code of {c.id}: {runSummary(c)}">
        <span class="obj-id">{c.id}</span>
        <span class="summary">{runSummary(c)}</span>
        <span class="who">{c.author}{c.rerun_of ? `, re-run of ${c.rerun_of}` : ""}</span>
      </button>
    </li>
  {/each}
</ul>
{#if shown.length > LIMIT}
  <button type="button" class="toggle-rejected" aria-expanded={all} onclick={() => (all = !all)}>
    {all ? "show fewer" : `show all (${shown.length})`}
  </button>
{/if}

<style>
  ul { list-style: none; margin: 0; padding: 0; }
  .run { margin-bottom: 4px; border-left: 3px solid var(--off); border-radius: 0 4px 4px 0; }
  .run.ok { border-left-color: var(--ok); }
  .run.failed { border-left-color: var(--error); }
  .run.running { border-left-color: var(--warn); }
  .run-btn { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; width: 100%; text-align: left; border: 0; background: transparent; color: var(--fg); font: inherit; font-size: 13px; padding: 4px 6px; cursor: pointer; }
  .run-btn:hover, .run-btn:focus-visible { outline: 2px solid var(--ann-claude); outline-offset: -2px; }
  .who { color: var(--muted); font-size: 11px; width: 100%; }
</style>
