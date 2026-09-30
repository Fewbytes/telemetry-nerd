<script lang="ts">
  import type { Gap } from "../lib/api";

  let { gaps = [] }: { gaps?: Gap[] } = $props();

  const metric = (g: Gap) =>
    `${g.suggestion.name}{${g.suggestion.labels.join(", ")}} (${g.suggestion.type})`;
</script>

<ul class="gaps">
  {#each gaps as gap (gap.id)}
    <li class="gap">
      <div class="missing">{gap.missing_signal}</div>
      <div class="needed">needed for: {gap.needed_for}</div>
      <div class="suggestion">suggested metric: <code>{metric(gap)}</code></div>
      <span class="badge author {gap.author}">{gap.author}</span>
    </li>
  {/each}
</ul>
