<script lang="ts">
  import type { Gap } from "../lib/api";

  let { gaps = [] }: { gaps?: Gap[] } = $props();

  const metric = (s: NonNullable<Gap["suggestion"]>) =>
    `${s.name}{${s.labels.join(", ")}} (${s.type})`;
</script>

<ul class="gaps">
  {#each gaps as gap (gap.id)}
    <li class="gap" id="gap-{gap.id}">
      <div class="missing">{gap.missing_signal}</div>
      <div class="needed">needed for: {gap.needed_for}</div>
      {#if gap.suggestion}
        <div class="suggestion">suggested metric: <code>{metric(gap.suggestion)}</code></div>
      {/if}
      <span class="badge author {gap.author}">{gap.author}</span>
    </li>
  {/each}
</ul>
